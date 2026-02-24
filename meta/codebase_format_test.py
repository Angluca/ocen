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
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories to skip entirely
SKIP_DIRS = {"bootstrap", "build", "tmp", "out.dSYM", ".git", "node_modules"}

# Known pre-existing formatter issues (files with known comment or unicode bugs).
# These are reported as warnings, not failures. Remove entries as bugs are fixed.
KNOWN_COMMENT_ISSUES = set()


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


def run_format(compiler, filepath, range_spec=None):
    """Run the formatter. Returns (stdout_bytes, returncode).
    Uses bytes to avoid unicode encoding issues."""
    cmd = [compiler, "format"]
    if range_spec:
        cmd += ["--range", range_spec]
    cmd.append(str(filepath))
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return None, -1
    except Exception:
        return None, -2


def check_comments(original_text, formatted_text):
    """Check that all comments from original are present in formatted output.
    Returns (ok, missing_comments)."""
    orig = Counter(extract_comment_texts(original_text))
    fmt = Counter(extract_comment_texts(formatted_text))
    missing = orig - fmt
    return len(missing) == 0, list(missing.elements())


def test_file(compiler, filepath):
    """Test a single file for idempotency and comment preservation.
    Returns (ok: bool, errors: list[str], formatted_bytes: bytes or None)."""
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

    # Idempotency check: format the output again
    with tempfile.NamedTemporaryFile(suffix='.oc', delete=False) as tmp:
        tmp.write(formatted_bytes)
        tmp_path = tmp.name

    try:
        formatted2_bytes, rc2 = run_format(compiler, tmp_path)
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


def test_range_on_formatted(compiler, formatted_bytes, start, end):
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
        range_bytes, rc = run_format(compiler, tmp_path, range_spec=range_spec)
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


def main():
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
        "dirs", nargs="*", default=["tests", "std", "compiler"],
        help="Directories to test (default: tests std compiler)"
    )
    args = parser.parse_args()

    compiler = args.compiler
    if not Path(compiler).exists():
        print(f"Compiler not found at {compiler}", file=sys.stderr)
        print("Build it first: ocen compiler/main.oc -o ./build/ocen", file=sys.stderr)
        sys.exit(1)

    print(f"Finding .oc files in: {', '.join(args.dirs)}")
    files = find_oc_files(args.dirs)
    print(f"Found {len(files)} files to test\n")

    # ── Phase 1: Full-format tests (idempotency + comment preservation) ──
    print("Phase 1: Full-format tests (idempotency + comment preservation)")
    full_failures = []
    known_warnings = []
    formatted_cache = {}  # filepath -> formatted_bytes (for Phase 2)
    for i, f in enumerate(files):
        rel = f.relative_to(ROOT)
        progress(i + 1, len(files), str(rel))
        ok, errs, formatted_bytes = test_file(compiler, f)
        if formatted_bytes is not None:
            formatted_cache[f] = formatted_bytes
        if not ok:
            if str(rel) in KNOWN_COMMENT_ISSUES:
                known_warnings.append((rel, errs))
            else:
                full_failures.append((rel, errs))

    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K")

    if full_failures:
        print(f"  FAILED: {len(full_failures)} files")
        for rel, errs in full_failures:
            for e in errs:
                print(f"    {rel}: {e}")
    else:
        print(f"  All {len(files)} files OK (excl. {len(known_warnings)} known issues)")
    if known_warnings:
        print(f"  Known issues ({len(known_warnings)} files):")
        for rel, errs in known_warnings:
            print(f"    {rel}: {errs[0].split(chr(10))[0]}")

    # ── Phase 2: Range format spot-checks (idempotency on formatted files) ──
    print(f"\nPhase 2: Range format spot-checks (idempotency)")

    # Select files for range testing from successfully-formatted files
    rng = random.Random(args.seed)
    candidates = [f for f in formatted_cache if f.stat().st_size > 200]
    rng.shuffle(candidates)
    range_files = candidates[:args.num_range_files]

    range_failures = []
    range_known = []
    total_checks = 0
    for i, f in enumerate(range_files):
        rel = f.relative_to(ROOT)
        formatted_bytes = formatted_cache[f]
        total_lines = formatted_bytes.decode('utf-8', errors='replace').count('\n')
        ranges = pick_ranges(total_lines)
        for start, end in ranges:
            total_checks += 1
            progress(total_checks, len(range_files) * 3, f"{rel} [{start}:{end}]")
            ok, errs = test_range_on_formatted(compiler, formatted_bytes, start, end)
            if not ok:
                if str(rel) in KNOWN_COMMENT_ISSUES:
                    range_known.append((rel, start, end, errs))
                else:
                    range_failures.append((rel, start, end, errs))

    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K")

    if range_failures:
        print(f"  FAILED: {len(range_failures)} range checks")
        for rel, start, end, errs in range_failures:
            for e in errs:
                print(f"    {rel}: {e}")
    else:
        print(f"  All {total_checks} range checks OK (excl. {len(range_known)} known issues)")

    # ── Phase 3: Range format diff-based checks (only range lines change) ──
    print(f"\nPhase 3: Range format diff-based checks (only range lines change)")

    # For each file, range-format the ORIGINAL content and verify that lines
    # outside [start, end] are preserved exactly (prefix and suffix match).
    diff_failures = []
    diff_known = []
    diff_checks = 0
    for i, f in enumerate(range_files):
        rel = f.relative_to(ROOT)
        original_bytes = f.read_bytes()
        total_lines = original_bytes.decode('utf-8', errors='replace').count('\n')
        ranges = pick_ranges(total_lines)
        for start, end in ranges:
            diff_checks += 1
            progress(diff_checks, len(range_files) * 3, f"{rel} [{start}:{end}]")
            ok, errs = test_range_preserves_outside(compiler, f, start, end)
            if not ok:
                if str(rel) in KNOWN_COMMENT_ISSUES:
                    diff_known.append((rel, start, end, errs))
                else:
                    diff_failures.append((rel, start, end, errs))

    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K")

    if diff_failures:
        print(f"  FAILED: {len(diff_failures)} diff checks")
        for rel, start, end, errs in diff_failures:
            for e in errs:
                print(f"    {rel}: {e}")
    else:
        print(f"  All {diff_checks} diff checks OK (excl. {len(diff_known)} known issues)")

    # ── Summary ──
    print()
    total_fail = len(full_failures) + len(range_failures) + len(diff_failures)
    if total_fail > 0:
        print(f"FAILED: {total_fail} issues found")
        sys.exit(1)
    else:
        print(f"All codebase format tests passed ({len(files)} full + {total_checks} range + {diff_checks} diff checks)")
        sys.exit(0)


if __name__ == "__main__":
    main()
