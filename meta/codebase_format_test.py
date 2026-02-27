#!/usr/bin/env python3
"""
Codebase format tests for the ocen formatter.

Runs the formatter on all .oc files in the given directories and checks:
  1. Idempotency: formatting twice produces identical output
  2. Comment preservation: all comments from the original are in the formatted output
  3. Range format spot-checks: for a sample of already-formatted files, range-format
     various regions and verify:
     a. All comments are still preserved
     b. The output is unchanged (range-formatting an already-formatted file is a no-op)
     c. Lines outside the range are preserved exactly (prefix/suffix match)
  4. Line-width tests (compiler/ and std/ only): format with widths [40, 80] and check
     idempotency, comment preservation, and validate syntax via LSP

Usage:
    python3 meta/codebase_format_test.py [-c COMPILER] [DIRS...]

    DIRS defaults to: tests std compiler
    COMPILER defaults to: ./build/ocen
"""

import os
import sys
import subprocess
import tempfile
import random
import argparse
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path

# Global list of (elapsed_ms, file_str, config_str) for every run_format call.
_timings: list[tuple[float, str, str]] = []
# Global list of (elapsed_ms, show_path_str) for every run_validate call.
_validate_timings: list[tuple[float, str]] = []
# When True, record and later print timing breakdowns. Set from CLI.
SHOW_TIMINGS = False
# Thread-safe progress counter
_progress_lock = threading.Lock()
_progress_count = 0
_progress_total = 0

ROOT = Path(__file__).resolve().parent.parent

# Directories to skip entirely
SKIP_DIRS = {"bootstrap", "build", "tmp", "out.dSYM", ".git", "node_modules"}


def find_oc_files(directories):
    """Find all .oc files in the given directories, skipping excluded ones."""
    files = []
    for d in directories:
        dirpath = ROOT / d
        if not dirpath.exists():
            print(f"Warning: {dirpath} does not exist", file=sys.stderr)
            continue
        for f in sorted(dirpath.rglob("*.oc")):
            rel = f.relative_to(ROOT)
            skip = any(sd in rel.parts for sd in SKIP_DIRS)
            if not skip:
                files.append(f)
    return files


def extract_comment_texts(text):
    """Extract all comment texts from source, handling strings carefully."""
    comments = []
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped.startswith('//'):
            comments.append(stripped)
            continue
        # Find inline comments: look for // not inside a string
        in_string = False
        string_char = None
        i = 0
        while i < len(line):
            c = line[i]
            if in_string:
                if c == '\\':
                    i += 2  # skip escaped char
                    continue
                if c == string_char:
                    in_string = False
            else:
                if c in ('"', '`'):
                    in_string = True
                    string_char = c
                elif c == '/' and i + 1 < len(line) and line[i+1] == '/':
                    comments.append(line[i:].strip())
                    break
            i += 1
    return comments


def run_format(compiler, filepath, range_spec=None, line_width=None,
               logical_path=None, run_label=None):
    """Run the formatter. Returns (stdout_bytes, returncode).
    Uses bytes to avoid unicode encoding issues.  Also records timing.

    logical_path: path recorded in _timings (defaults to filepath).
    run_label:    short suffix appended to config_label, e.g. "idem".
    """
    cmd = [compiler, "format"]
    if line_width is not None:
        cmd += ["--line-width", str(line_width)]
    if range_spec:
        cmd += ["--range", range_spec]
    cmd.append(str(filepath))
    # Build a human-readable config label for benchmarking
    config_parts = []
    if line_width is not None:
        config_parts.append(f"width={line_width}")
    if range_spec:
        config_parts.append(f"range={range_spec}")
    config_label = ", ".join(config_parts) if config_parts else "default"
    if run_label:
        config_label = f"{config_label} [{run_label}]"
    record_path = str(logical_path) if logical_path is not None else str(filepath)
    try:
        t0 = time.perf_counter()
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if SHOW_TIMINGS:
            _timings.append((elapsed_ms, record_path, config_label))
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        if SHOW_TIMINGS:
            _timings.append((30_000.0, record_path, config_label + " [TIMEOUT]"))
        return None, -1
    except Exception:
        return None, -2


def run_validate(compiler, formatted_bytes, show_path):
    """Validate formatted output using LSP --validate.
    Writes formatted_bytes to a temp file, then runs:
      compiler lsp --validate <tmpfile> --show-path <show_path>
    Returns (ok: bool, errors: list[str])."""
    errors = []
    with tempfile.NamedTemporaryFile(suffix='.oc', delete=False) as tmp:
        tmp.write(formatted_bytes)
        tmp_path = tmp.name

    try:
        cmd = [compiler, "lsp", "--validate", tmp_path, "--show-path", str(show_path)]
        t0 = time.perf_counter()
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if SHOW_TIMINGS:
            _validate_timings.append((elapsed_ms, str(show_path)))
        output = result.stdout.decode('utf-8', errors='replace').strip()
        if output:
            import json
            for line in output.split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    diag = json.loads(line)
                    if diag.get("severity") == "Error":
                        msg = diag.get("message", "")
                        # "Must yield a value in this branch, body type is Block"
                        # indicates the formatter produced invalid expression blocks
                        if "Must yield a value" in msg and "Block" in msg:
                            span = diag.get("span", {})
                            loc = f"line {span.get('start_line', '?')}"
                            errors.append(f"Validation error at {loc}: {msg}")
                except json.JSONDecodeError:
                    pass
    except subprocess.TimeoutExpired:
        pass  # Skip validation on timeout
    except Exception:
        pass  # Skip validation on error
    finally:
        os.unlink(tmp_path)

    return len(errors) == 0, errors


def check_comments(original_text, formatted_text):
    """Check that all comments from original are present in formatted output.
    Returns (ok, missing_comments)."""
    orig = Counter(extract_comment_texts(original_text))
    fmt = Counter(extract_comment_texts(formatted_text))
    missing = orig - fmt
    return len(missing) == 0, list(missing.elements())


def count_blank_line_gaps(text):
    """Count the number of blank line separator groups in the text.

    A gap is one or more consecutive blank lines between non-blank content.
    Leading/trailing blank lines are ignored.

    Returns the count of gap groups."""
    lines = text.split('\n')
    # Strip leading blank lines
    while lines and lines[0].strip() == '':
        lines.pop(0)
    # Strip trailing blank lines
    while lines and lines[-1].strip() == '':
        lines.pop()

    gaps = 0
    in_gap = False
    for line in lines:
        if line.strip() == '':
            if not in_gap:
                gaps += 1
                in_gap = True
        else:
            in_gap = False
    return gaps


def check_blank_line_preservation(original_text, formatted_text):
    """Check that blank line separators from the original are preserved.

    If there was a blank line (or multiple) between two sections of code
    in the original, there should still be at least one blank line between
    them in the formatted output.

    Returns (ok, lost_count, orig_gaps, fmt_gaps)."""
    orig_gaps = count_blank_line_gaps(original_text)
    fmt_gaps = count_blank_line_gaps(formatted_text)

    lost = orig_gaps - fmt_gaps
    if lost > 0:
        return False, lost, orig_gaps, fmt_gaps
    return True, 0, orig_gaps, fmt_gaps


def test_file(compiler, filepath, check_idem=True):
    """Test a single file for idempotency and comment preservation.
    Returns (ok: bool, errors: list[str], formatted_bytes: bytes or None).
    If check_idem is False, skip the idempotency check (format-twice)."""
    errors = []

    original_bytes = filepath.read_bytes()
    original_text = original_bytes.decode('utf-8', errors='replace')

    # First format
    formatted_bytes, rc = run_format(compiler, filepath)
    if rc != 0:
        # File doesn't format successfully — skip
        return True, [], None

    formatted_text = formatted_bytes.decode('utf-8', errors='replace')

    # Comment preservation check
    comments_ok, missing = check_comments(original_text, formatted_text)
    if not comments_ok:
        msgs = [f"  missing: {c}" for c in missing[:5]]
        errors.append("Comments lost:\n" + "\n".join(msgs))

    # Blank line preservation check
    blank_ok, blank_lost, orig_gaps, fmt_gaps = check_blank_line_preservation(original_text, formatted_text)
    if not blank_ok:
        errors.append(f"Blank line separators lost: {blank_lost} (original: {orig_gaps}, formatted: {fmt_gaps})")

    # Idempotency check: format the output again
    # If output == input, idempotency is trivially satisfied — skip the subprocess call
    if check_idem and formatted_bytes != original_bytes:
        with tempfile.NamedTemporaryFile(suffix='.oc', delete=False) as tmp:
            tmp.write(formatted_bytes)
            tmp_path = tmp.name

        try:
            formatted2_bytes, rc2 = run_format(compiler, tmp_path,
                                               logical_path=filepath, run_label="idem")
        finally:
            os.unlink(tmp_path)

        if rc2 != 0:
            errors.append(f"Second format crashed (exit {rc2})")
        elif formatted2_bytes != formatted_bytes:
            lines1 = formatted_text.split('\n')
            lines2 = formatted2_bytes.decode('utf-8', errors='replace').split('\n')
            for i, (l1, l2) in enumerate(zip(lines1, lines2)):
                if l1 != l2:
                    errors.append(f"Not idempotent at line {i+1}: '{l1[:80]}' -> '{l2[:80]}'")
                    break
            else:
                if len(lines1) != len(lines2):
                    errors.append(f"Not idempotent: line count {len(lines1)} vs {len(lines2)}")

    return len(errors) == 0, errors, formatted_bytes


def test_file_with_width(compiler, filepath, line_width, validate=False, check_idem=True):
    """Test a single file for comment preservation and idempotency with --line-width.
    Optionally validates syntax of formatted output using LSP.
    Returns (ok: bool, errors: list[str])."""
    errors = []

    original_bytes = filepath.read_bytes()
    original_text = original_bytes.decode('utf-8', errors='replace')

    # First format with line-width
    formatted_bytes, rc = run_format(compiler, filepath, line_width=line_width)
    if rc != 0:
        # File doesn't format successfully — skip
        return True, []

    formatted_text = formatted_bytes.decode('utf-8', errors='replace')

    # Comment preservation check
    comments_ok, missing = check_comments(original_text, formatted_text)
    if not comments_ok:
        msgs = [f"  missing: {c}" for c in missing[:5]]
        errors.append(f"[width={line_width}] Comments lost:\n" + "\n".join(msgs))

    # Blank line preservation check
    blank_ok, blank_lost, orig_gaps, fmt_gaps = check_blank_line_preservation(original_text, formatted_text)
    if not blank_ok:
        errors.append(f"[width={line_width}] Blank line separators lost: {blank_lost} (original: {orig_gaps}, formatted: {fmt_gaps})")

    # Idempotency check: skip if output is identical to input (trivially idempotent)
    if check_idem and formatted_bytes != original_bytes:
        with tempfile.NamedTemporaryFile(suffix='.oc', delete=False) as tmp:
            tmp.write(formatted_bytes)
            tmp_path = tmp.name

        try:
            formatted2_bytes, rc2 = run_format(compiler, tmp_path, line_width=line_width,
                                               logical_path=filepath, run_label="idem")
        finally:
            os.unlink(tmp_path)

        if rc2 != 0:
            errors.append(f"[width={line_width}] Second format crashed (exit {rc2})")
        elif formatted2_bytes != formatted_bytes:
            lines1 = formatted_text.split('\n')
            lines2 = formatted2_bytes.decode('utf-8', errors='replace').split('\n')
            for i, (l1, l2) in enumerate(zip(lines1, lines2)):
                if l1 != l2:
                    errors.append(f"[width={line_width}] Not idempotent at line {i+1}: '{l1[:80]}' -> '{l2[:80]}'")
                    break
            else:
                if len(lines1) != len(lines2):
                    errors.append(f"[width={line_width}] Not idempotent: line count {len(lines1)} vs {len(lines2)}")

    # Syntax validation: skip if output matches input (original is known-valid)
    if validate and len(errors) == 0 and formatted_bytes != original_bytes:
        # Use the original filepath as --show-path so the LSP resolves imports correctly
        rel = filepath.relative_to(ROOT)
        val_ok, val_errs = run_validate(compiler, formatted_bytes, f"./{rel}")
        if not val_ok:
            for e in val_errs:
                errors.append(f"[width={line_width}] {e}")

    return len(errors) == 0, errors


def pick_ranges(total_lines):
    """Pick a few interesting line ranges for spot-check range formatting.
    Returns a list of (start, end) tuples."""
    if total_lines < 10:
        return []

    ranges = []

    # Range 1: first ~25% of the file
    quarter = max(3, total_lines // 4)
    ranges.append((1, quarter))

    # Range 2: middle chunk
    mid = total_lines // 2
    chunk = max(3, total_lines // 6)
    ranges.append((max(1, mid - chunk // 2), min(total_lines, mid + chunk // 2)))

    # Range 3: last ~25% of the file
    ranges.append((max(1, total_lines - quarter), total_lines))

    return ranges


def test_range_on_formatted(compiler, formatted_bytes, start, end, logical_path=None):
    """Test range formatting on an already-formatted file.
    Since the file is already formatted, range-formatting any region should
    produce identical output (idempotency). Also checks comment preservation.
    Returns (ok: bool, errors: list[str])."""
    errors = []
    formatted_text = formatted_bytes.decode('utf-8', errors='replace')

    with tempfile.NamedTemporaryFile(suffix='.oc', delete=False) as tmp:
        tmp.write(formatted_bytes)
        tmp_path = tmp.name

    try:
        range_spec = f"{start}:{end}"
        range_bytes, rc = run_format(compiler, tmp_path, range_spec=range_spec,
                                     logical_path=logical_path)
    finally:
        os.unlink(tmp_path)

    if rc != 0:
        # Can't format — skip
        return True, []

    range_text = range_bytes.decode('utf-8', errors='replace')

    # Check 1: output should be identical (range on already-formatted = no-op)
    if range_bytes != formatted_bytes:
        # Find first difference
        lines1 = formatted_text.split('\n')
        lines2 = range_text.split('\n')
        for i, (l1, l2) in enumerate(zip(lines1, lines2)):
            if l1 != l2:
                errors.append(
                    f"Range {range_spec} not idempotent at line {i+1}: "
                    f"'{l1[:60]}' -> '{l2[:60]}'"
                )
                break
        else:
            if len(lines1) != len(lines2):
                errors.append(
                    f"Range {range_spec} not idempotent: "
                    f"line count {len(lines1)} vs {len(lines2)}"
                )

    # Check 2: all comments preserved
    comments_ok, missing = check_comments(formatted_text, range_text)
    if not comments_ok:
        msgs = [f"  missing: {c}" for c in missing[:3]]
        errors.append(f"Range {range_spec}: comments lost:\n" + "\n".join(msgs))

    return len(errors) == 0, errors


def test_range_preserves_outside(compiler, filepath, start, end):
    """Test that range formatting only modifies lines within [start, end].
    Lines before `start` and after `end` in the original must appear as an
    exact prefix and suffix (respectively) of the formatted output.
    Returns (ok: bool, errors: list[str])."""
    errors = []
    original_bytes = filepath.read_bytes()
    original_text = original_bytes.decode('utf-8', errors='replace')
    original_lines = original_text.split('\n')
    # Remove trailing empty element from trailing newline for comparison
    if original_lines and original_lines[-1] == '':
        original_lines = original_lines[:-1]

    range_spec = f"{start}:{end}"
    range_bytes, rc = run_format(compiler, filepath, range_spec=range_spec)
    if rc != 0:
        return True, []  # skip files that don't format

    range_text = range_bytes.decode('utf-8', errors='replace')
    range_lines = range_text.split('\n')
    # Remove trailing empty element from trailing newline for comparison
    if range_lines and range_lines[-1] == '':
        range_lines = range_lines[:-1]

    # Check prefix: lines 1..start-1 must be identical
    prefix_count = start - 1
    for i in range(min(prefix_count, len(original_lines), len(range_lines))):
        if original_lines[i] != range_lines[i]:
            errors.append(
                f"Range {range_spec}: prefix differs at line {i+1}: "
                f"'{original_lines[i][:60]}' -> '{range_lines[i][:60]}'"
            )
            break

    if prefix_count > len(range_lines):
        errors.append(
            f"Range {range_spec}: output too short for prefix "
            f"({len(range_lines)} < {prefix_count})"
        )

    # Check suffix: lines end+1..EOF must appear as the last lines of output.
    # The number of suffix lines from the original is (total_original - end).
    suffix_count = len(original_lines) - end
    if suffix_count > 0 and suffix_count <= len(range_lines):
        orig_suffix = original_lines[end:]
        range_suffix = range_lines[len(range_lines) - suffix_count:]
        for i, (o, r) in enumerate(zip(orig_suffix, range_suffix)):
            if o != r:
                orig_line = end + 1 + i
                errors.append(
                    f"Range {range_spec}: suffix differs at original line {orig_line}: "
                    f"'{o[:60]}' -> '{r[:60]}'"
                )
                break

    return len(errors) == 0, errors


def progress(i, total, msg):
    if sys.stdout.isatty():
        sys.stdout.write(f"\r\033[2K[{i}/{total}] {msg[:70]:<70}")
        sys.stdout.flush()


def tick_progress(msg=""):
    """Thread-safe progress update."""
    global _progress_count
    with _progress_lock:
        _progress_count += 1
        count = _progress_count
        total = _progress_total
    if sys.stdout.isatty():
        sys.stdout.write(f"\r\033[2K[{count}/{total}] {msg[:70]:<70}")
        sys.stdout.flush()


def main():
    global _progress_count, _progress_total

    parser = argparse.ArgumentParser(
        description="Codebase format tests: idempotency, comment preservation, and range formatting"
    )
    parser.add_argument(
        "-c", "--compiler", default=str(ROOT / "build" / "ocen"),
        help="Path to the compiler executable"
    )
    parser.add_argument(
        "-s", "--seed", type=int, default=42,
        help="Random seed for range spot-check file selection (default: 42)"
    )
    parser.add_argument(
        "-n", "--num-range-files", type=int, default=30,
        help="Number of files to spot-check with range formatting (default: 30)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=0,
        help="Number of parallel workers (default: cpu_count)"
    )
    parser.add_argument(
        "--timings", action="store_true",
        help="Show benchmark timing breakdown (format/validate)"
    )
    parser.add_argument(
        "dirs", nargs="*", default=["tests", "std", "compiler"],
        help="Directories to test (default: tests std compiler)"
    )
    args = parser.parse_args()

    # control whether we record/print timing breakdowns
    global SHOW_TIMINGS
    SHOW_TIMINGS = args.timings

    compiler = args.compiler
    if not Path(compiler).exists():
        print(f"Compiler not found at {compiler}", file=sys.stderr)
        print("Build it first: ocen compiler/main.oc -o ./build/ocen", file=sys.stderr)
        sys.exit(1)

    max_workers = args.jobs if args.jobs > 0 else max((os.cpu_count() or 4) * 3 // 2, 4)

    print(f"Finding .oc files in: {', '.join(args.dirs)}")
    files = find_oc_files(args.dirs)

    # Pre-compute Phase 3 file list (independent of Phase 1)
    LINE_WIDTHS = [40, 80]
    WIDTH_DIRS = ["compiler", "std"]
    width_files = find_oc_files(WIDTH_DIRS)
    width_tasks = [(f, lw) for f in width_files for lw in LINE_WIDTHS]

    print(f"Found {len(files)} files to test (workers: {max_workers})\n")

    # ── Phase 1+2: Run full-format and line-width tests concurrently ──
    print("Phase 1: Full-format tests (idempotency + comment preservation + blank lines)")
    print(f"Phase 2: Line-width tests (widths: {LINE_WIDTHS}, dirs: {WIDTH_DIRS})")
    phase13_t0 = time.perf_counter()

    full_failures = []
    formatted_cache = {}  # filepath -> formatted_bytes (for Phase 2)
    width_failures = []
    width_checks = len(width_tasks)

    _progress_count = 0
    _progress_total = len(files) + len(width_tasks)

    # Skip idempotency for tests/ files — their purpose here is crash/comment testing.
    # Idempotency is thoroughly tested via tests/format/ in the unit test suite.
    tests_dir = ROOT / 'tests'

    def _phase1_task(f):
        rel = f.relative_to(ROOT)
        idem = not f.is_relative_to(tests_dir)
        ok, errs, formatted_bytes = test_file(compiler, f)
        tick_progress(str(rel))
        return ('p1', f, rel, ok, errs, formatted_bytes)

    # Only validate compiler/ and std/ files
    compiler_dir = ROOT / 'compiler'
    std_dir = ROOT / 'std'

    def _phase2_task(task):
        f, lw = task
        rel = f.relative_to(ROOT)
        is_compiler = f.is_relative_to(compiler_dir)
        is_std = f.is_relative_to(std_dir)
        ok, errs = test_file_with_width(compiler, f, lw,
                                        validate=is_compiler or is_std)
        tick_progress(f"{rel} [width={lw}]")
        return ('p2', None, rel, ok, errs, None)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for f in files:
            futures.append(executor.submit(_phase1_task, f))
        for t in width_tasks:
            futures.append(executor.submit(_phase2_task, t))
        for future in as_completed(futures):
            tag, f, rel, ok, errs, formatted_bytes = future.result()
            if tag == 'p1':
                if formatted_bytes is not None:
                    formatted_cache[f] = formatted_bytes
                if not ok:
                    full_failures.append((rel, errs))
            else:  # p2
                if not ok:
                    width_failures.append((rel, errs))

    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K")

    phase12_elapsed = time.perf_counter() - phase13_t0
    if full_failures:
        print(f"  Phase 1 FAILED: {len(full_failures)} files  ({phase12_elapsed:.1f}s)")
        for rel, errs in full_failures:
            for e in errs:
                print(f"    {rel}: {e}")
    else:
        print(f"  Phase 1: All {len(files)} files OK  ({phase12_elapsed:.1f}s)")

    if width_failures:
        print(f"  Phase 2 FAILED: {len(width_failures)} width checks  ({phase12_elapsed:.1f}s)")
        for rel, errs in width_failures:
            for e in errs:
                print(f"    {rel}: {e}")
    else:
        print(f"  Phase 2: All {width_checks} width checks OK  ({phase12_elapsed:.1f}s)")

    # ── Phase 3: Range format spot-checks (idempotency + diff) ──
    print(f"\nPhase 3: Range format spot-checks (idempotency + diff)")
    phase2_t0 = time.perf_counter()

    # Select files for range testing from successfully-formatted files
    rng = random.Random(args.seed)
    candidates = sorted([f for f in formatted_cache if f.stat().st_size > 200])
    rng.shuffle(candidates)
    range_files = candidates[:args.num_range_files]

    # Build all range tasks upfront
    range_tasks = []   # (f, rel, start, end, 'range'|'diff')
    for f in range_files:
        rel = f.relative_to(ROOT)
        formatted_bytes = formatted_cache[f]
        total_lines = formatted_bytes.decode('utf-8', errors='replace').count('\n')
        ranges = pick_ranges(total_lines)
        for start, end in ranges:
            range_tasks.append((f, rel, start, end, 'range'))

        original_bytes = f.read_bytes()
        orig_total_lines = original_bytes.decode('utf-8', errors='replace').count('\n')
        orig_ranges = pick_ranges(orig_total_lines)
        for start, end in orig_ranges:
            range_tasks.append((f, rel, start, end, 'diff'))

    _progress_count = 0
    _progress_total = len(range_tasks)

    range_failures = []
    total_range_checks = 0
    total_diff_checks = 0

    def _phase3_task(task):
        f, rel, start, end, kind = task
        if kind == 'range':
            ok, errs = test_range_on_formatted(compiler, formatted_cache[f], start, end,
                                               logical_path=f)
        else:
            ok, errs = test_range_preserves_outside(compiler, f, start, end)
        tick_progress(f"{rel} [{start}:{end}] {kind}")
        return rel, start, end, kind, ok, errs

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_phase3_task, t) for t in range_tasks]
        for future in as_completed(futures):
            rel, start, end, kind, ok, errs = future.result()
            if kind == 'range':
                total_range_checks += 1
            else:
                total_diff_checks += 1
            if not ok:
                range_failures.append((rel, start, end, errs))

    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K")

    phase2_elapsed = time.perf_counter() - phase2_t0
    if range_failures:
        print(f"  FAILED: {len(range_failures)} range/diff checks  ({phase2_elapsed:.1f}s)")
        for rel, start, end, errs in range_failures:
            for e in errs:
                print(f"    {rel}: {e}")
    else:
        print(f"  All {total_range_checks} range + {total_diff_checks} diff checks OK  ({phase2_elapsed:.1f}s)")

    # ── Benchmark report: top 20 slowest configs ──
    if args.timings and (_timings or _validate_timings):
        print()
        print("Benchmark: top 20 slowest format invocations")
        sorted_timings = sorted(_timings, key=lambda t: t[0], reverse=True)
        top20 = sorted_timings[:20]
        if top20:
            max_file_len = max(len(Path(f).name) for _, f, _ in top20)
            max_cfg_len  = max(len(c) for _, _, c in top20)
            print(f"  {'#':>3}  {'ms':>8}  {'config':<{max_cfg_len}}  file")
            print(f"  {'-'*3}  {'-'*8}  {'-'*max_cfg_len}  {'-'*40}")
            for rank, (ms, fpath, cfg) in enumerate(top20, 1):
                rel = str(Path(fpath).relative_to(ROOT)) if fpath.startswith(str(ROOT)) else fpath
                print(f"  {rank:>3}  {ms:>8.1f}  {cfg:<{max_cfg_len}}  {rel}")
        print()
        total_ms = sum(t[0] for t in _timings)
        print(f"  Format: {total_ms/1000:.2f}s across {len(_timings)} invocations (mean {total_ms/len(_timings):.1f}ms)")

        if _validate_timings:
            print()
            print("Benchmark: top 20 slowest validate invocations")
            sorted_vtimings = sorted(_validate_timings, key=lambda t: t[0], reverse=True)
            top20v = sorted_vtimings[:20]
            max_vfile_len = max(len(Path(f).name) for _, f in top20v)
            print(f"  {'#':>3}  {'ms':>8}  file")
            print(f"  {'-'*3}  {'-'*8}  {'-'*60}")
            for rank, (ms, fpath) in enumerate(top20v, 1):
                rel = str(Path(fpath).relative_to(ROOT)) if fpath.startswith(str(ROOT)) else fpath
                print(f"  {rank:>3}  {ms:>8.1f}  {rel}")
            print()
            vtotal_ms = sum(t[0] for t in _validate_timings)
            print(f"  Validate: {vtotal_ms/1000:.2f}s across {len(_validate_timings)} invocations (mean {vtotal_ms/len(_validate_timings):.1f}ms)")
            print(f"  Total measured (format+validate): {(total_ms+vtotal_ms)/1000:.2f}s")

    # ── Summary ──
    print()
    total_fail = len(full_failures) + len(range_failures) + len(width_failures)
    if total_fail > 0:
        print(f"FAILED: {total_fail} issues found")
        sys.exit(1)
    else:
        print(f"All codebase format tests passed ({len(files)} full + {total_range_checks} range + {total_diff_checks} diff + {width_checks} width checks)")
        sys.exit(0)


if __name__ == "__main__":
    main()
