# Ocen Formatter Design

## Overview

The ocen formatter (`ocen format <file.oc>`) is an AST-based code formatter that
reads an ocen source file, formats it according to the project's style guide
(`docs/CODE_STYLE.md`), and outputs the result to stdout. It is implemented
entirely in `compiler/formatter.oc`.

Key properties:
- **AST-based**: Operates on the parsed AST, not raw tokens
- **Comment-preserving**: Every comment in the source appears in the output
- **Idempotent**: Formatting twice is identical to formatting once
- **Non-destructive**: Output goes to stdout; the original file is unchanged
- **Range-capable**: `--range S:E` limits changes to declarations/statements that overlap a line range
- **Width-aware**: Optional `--line-width N` enables line-length-driven breaking

---

## Command-Line Interface

```
ocen format [options] <file>

Options:
    --indent N       Set indent size in spaces (default: 4)
    --line-width N   Set target line width for group breaking (default: 0 = disabled)
    --range S:E      Only format declarations overlapping lines S through E (1-based)
    -h, --help       Display this information
```

The subcommand is dispatched from `compiler/main.oc` to `format_main()` in
`compiler/formatter.oc`. If the file cannot be found, or if parsing fails, the
formatter prints the original source unchanged and exits 0 (graceful fallback).
This is important for editor integration: a syntax error in the file should not
corrupt the document.

---

## Parsing Pipeline

The formatter uses a simplified, format-specific parse pipeline:

1. Read the file from disk
2. Create a `Program` with `include_stdlib = false` and `format_mode = true`
3. Install a `setjmp` error context so lexer/parser panics are caught
4. Parse via `Parser::parse_toplevel()` — produces AST + comments, no typechecking
5. If parsing produced errors, fall back to emitting original source unchanged

The `format_mode` flag suppresses certain parser behaviours that are only needed
for compilation (e.g., some synthesized nodes). Setting `include_stdlib = false`
prevents the stdlib from being parsed into the same namespace.

---

## Relevant Source Files

| File | Role |
|------|------|
| `compiler/formatter.oc` | Entire formatter implementation |
| `compiler/main.oc` | `format` subcommand dispatch |
| `compiler/lexer.oc` | Modified to save all comments into `Program.comments` |
| `compiler/tokens.oc` | `Comment` struct (`text`, `span`, `is_doc`, `is_inline`) |
| `compiler/ast/program.oc` | `comments: &Vector<Comment>` field on `Program` |
| `compiler/parser.oc` | Copies comments from `Lexer` to `Program` after parsing |

---

## Core Architecture

### `Formatter` Struct

The central state object. Key fields:

| Field | Purpose |
|-------|---------|
| `output: Buffer` | Growing output buffer |
| `indent: u32` | Current indentation level |
| `options: FormatOptions` | `indent_size`, `line_width` |
| `source: str` | Original source text (for hybrid source reads) |
| `line_offsets: &Vector<u32>` | Byte offset of each source line (for blank-line detection) |
| `filename: str` | Used to filter comments from other files |
| `comments: &Vector<Comment>` | Pre-filtered comments for this file only |
| `comment_index: u32` | Forward cursor into `comments` |
| `comment_emitted: &Vector<bool>` | Tracks which comments have been written |
| `comment_line_index: CommentIndex` | Per-line O(1) index for backward comment lookups |
| `output_line: u32` | Current 1-based line number in `output` |
| `output_col: u32` | Current 0-based column in `output` |
| `range_start/end: u32` | Active range for `--range` mode (0 = full format) |
| `decl_mappings: &Vector<DeclMapping>` | Source↔output line maps per declaration |
| `stmt_mappings: &Vector<DeclMapping>` | Source↔output line maps per statement |
| `track_stmts: bool` | Whether `format_block` should record `stmt_mappings` |
| `ic_lines: &Vector<u32>` | Output line numbers that end with inline comments (for alignment) |
| `in_format_str: bool` | Suppresses multi-line formatting inside format strings / measurement |

### `FormatOptions`

Centralises all tunable parameters:
- `indent_size: u32` — spaces per indent level (default 4)
- `line_width: u32` — target line width; 0 means disabled (default 0)

`FormatOptions::width_enabled()` returns true when `line_width > 0`.

---

## Source Order Reconstruction

The AST `Namespace` stores declarations in separate typed vectors (functions,
structs, enums, variables, constants, imports, namespaces, typedefs, compiler
options), losing source order. `Formatter::collect_decls()` gathers all of them
into a single `Vector<Decl>` and sorts by `(line, col)` from each item's
`span.start`. All subsequent formatting iterates this sorted vector.

---

## Hybrid AST + Source Text

The formatter primarily uses AST structure to decide layout, but falls back to
the original source text (via `Program.get_source_text(span)`) for faithful
reproduction of details that the AST does not distinguish:

- String literal delimiters (`"…"` vs `` `…` `` vs `f"…"`)
- Numeric literal formats (hex, binary, suffixes like `u32`)
- Whether a function body used `then` vs braces
- Compiler directive argument text

---

## Comment Preservation

### Collection

The lexer's `next_token()` is modified so every `//…` comment produces a
`Comment` value (rather than being discarded). After parsing, `Parser` moves the
lexer's comment list to `Program.comments`. Comments are sorted in source order.

Each `Comment` carries:
- `text`: full text including the `//` leader
- `span`: source location
- `is_doc`: true for `///`, `//!`, `//*`, `//.` leaders
- `is_inline`: true when non-whitespace code appeared before `//` on the same line

### Filtering

`Formatter::make()` filters `program.comments` to only those whose
`span.start.filename` matches the file being formatted. This prevents comments
from transitively-parsed files from leaking into the output.

### Forward Cursor (`comment_index`)

Most comment emission is linear. The formatter maintains a forward cursor
`comment_index` into the filtered comment array. Two primary methods drive it:

- **`emit_comments_before(line)`** — emits all non-inline comments whose line
  is before `line`, advancing the cursor. When `preserve_blanks = true`, a
  blank line is inserted between non-adjacent comment groups.

- **`emit_inline_comment(line)`** — emits any inline comments exactly on `line`,
  advancing the cursor over them.

### Backward Lookups (`CommentIndex`)

Some AST nodes are visited out of strict source order (e.g., struct fields
interspersed with multi-field groups, or range-mode's reorganised rendering).
When `emit_inline_comment()` is called for a line *earlier* than the cursor's
current position (a "cursor regression"), it uses `comment_line_index` to do an
O(k) indexed backward lookup (where k = comments on that line) via
`emit_inline_comment_backward()`. The `comment_emitted` boolean array prevents
double-emission.

`CommentIndex::build()` pre-allocates a vector-of-vectors indexed by line number,
so any per-line lookup is O(1) + O(k).

### Tracking Emission

Every emitted comment sets `comment_emitted[i] = true`. At the end of `run()`,
the formatter asserts `comment_index == comments.size` and logs a warning for
any comment not marked emitted. This catches both cursor bugs and missed
comment sites.

### Inline Comment Alignment (Post-Processing)

After the full output is generated, `align_inline_comments()` groups consecutive
output lines that end with inline comments and pads each line's comment to the
rightmost column in the group. The `ic_lines` vector records output line numbers
where inline comments were emitted; the post-processor scans for consecutive runs
of ≥ 2 entries, finds the maximum column within each run via
`find_inline_comment_col()`, and inserts spaces before `//` in lines that are
shorter.

---

## Formatting Decisions

### Pure Decision Functions

Formatting policy is separated from AST traversal via standalone pure functions:

| Function | Policy |
|----------|--------|
| `should_break_collection` | Break call args / array items if multi-line in source **and** has inline comments |
| `should_break_list_literal` | Break `$[…]` / `${…}` literals if multi-line in source (always, even without comments) |
| `should_break_params` | Break function params if multi-line in source **and** has inline comments |
| `should_break_binary_rhs` | Place RHS on next line if it starts on a different line than the operator |

All of these accept `&FormatOptions` so the interface is ready to incorporate
width budgets in future without changing callers.

### Plan / Emit Separation

Formatting decisions are captured as plain data structs before any text is
emitted:

- `CollectionPlan { multiline, size, open, close }` — for call arguments,
  array/map literals, etc.
- `ParamPlan { multiline, param_count, is_variadic }` — for function parameter
  lists.

The `plan_collection` / `plan_params` functions build the plan; `emit_collection`
/ `emit_params` consume it. No decisions are made during emission.

### Width-Aware Formatting

When `line_width > 0`, the formatter can measure proposed output widths before
committing them:

- **`measure_expr(node)`** — temporarily swaps the output buffer, formats the
  expression with `in_format_str = true` (suppresses multiline), and returns the
  first-line width. Comment state is saved and restored so the measurement pass
  has no side effects.
- **`measure_statement`**, **`measure_collection`**, **`measure_params`** —
  analogous buffer-capture measurements.
- **`would_exceed_width(additional)`** — true if `output_col + additional > line_width`.
- **`remaining_width()`** — available columns on the current line.

Buffer capture saves/restores the entire `CommentState` struct (cursor index,
`last_comment_request_line`, `cursor_regressions`, and a deep copy of
`comment_emitted`), so measurements are fully non-destructive.

### Doc IR Layer (Wadler-Lindig)

A Prettier-style intermediate representation is available for constructs where
full width-aware breaking is desired:

```
enum DocKind { Text, Line, SoftLine, SoftEmpty, Concat, DocIndent, Group, IfBreak }
```

- **`Group`**: try to fit children on one line in `Flat` mode; switch to `Break`
  mode (expanding `SoftLine`→newline etc.) if the flat width exceeds `line_width`.
- **`SoftLine`**: space in flat mode, newline+indent in break mode.
- **`SoftEmpty`**: nothing in flat mode, newline+indent in break mode.
- **`IfBreak`**: choose between two alternative docs based on mode.

`doc_flat_width()` computes the flat width recursively (returning -1 for any doc
containing a hard `Line`).

`doc_render(doc, indent_size, line_width)` implements the stack-based
Wadler-Lindig algorithm: for each `Group`, it calls `doc_flat_width` to check
if the flat version fits in the remaining line width; if so it renders in
`Flat` mode, otherwise `Break` mode.

`build_collection_doc()` demonstrates the full Doc IR pipeline: it builds a
`Group` node for a delimited, comma-separated list and renders it with
`doc_render`.

---

## Top-Level Formatting Loop (`format_ns`)

1. Calls `collect_decls()` to obtain declarations in source order.
2. Iterates declarations, inserting blank lines between groups with the rule:
   - No blank between consecutive `import` declarations
   - No blank between consecutive `@compiler` directives
   - No blank between consecutive `let`/`const` declarations
   - Between consecutive arrow functions: use actual source blank lines
     (`has_blank_source_line_between()`) rather than line-number gaps
   - One blank line between everything else
3. Before each declaration, calls `emit_comments_before(decl.line)` to flush
   any preceding standalone comments.
4. After the last declaration, calls `emit_remaining_comments()` to output any
   trailing comments.
5. When `track_mappings = true` (range mode), records a `DeclMapping` for each
   declaration mapping its source line range to its output line range.

`emit_item_preamble()` is a consolidated helper that handles the blank-line and
comment-preamble logic shared by `format_block` (statements) and `format_struct`
(fields).

---

## Statement and Expression Formatting

### `format_block`

Formats a `{ … }` body. Iterates statements, calling `emit_item_preamble()` for
each and then `format_statement()`. When `track_stmts = true` (enabled inside
functions during range mode), each statement's source and output line extents are
recorded in `stmt_mappings`.

### `format_statement` / `format_expr`

Direct recursive descent over the AST. Key handling notes:

- **Arrow functions**: When `width_enabled()`, the body width is measured; if it
  would exceed the line width from the current column, emit `=>` + newline +
  indented body instead of `=> body` on the same line.
- **For-each loops**: The parser desugars `for x in coll { … }` into a C-style
  loop with a generated iterator variable named `_iN`. The formatter detects this
  pattern (init variable name starts with `_i`) and reconstructs `for x in coll`.
- **Binary operators with line-break RHS**: If the RHS starts on a different
  source line than the operator, the formatter emits a newline before the RHS
  (respecting indent).
- **String literals**: Source text is used verbatim for the delimiter/prefix so
  `"…"`, `` `…` ``, `f"…"` are all preserved exactly.
- **Format strings** (`f"…${expr}…"`): `in_format_str` is set during expression
  formatting inside format strings to suppress multiline decisions.
- **Multi-line VectorLiteral / MapLiteral**: `should_break_list_literal` returns
  true whenever the construct spans multiple source lines, regardless of comments.

### Return Type Detection

The parser synthesizes a return type for functions lacking an explicit one:
- `def foo()` → synthesized `Void` type
- `def main()` → synthesized `I32` type with a span at the function name position

`has_explicit_return_type()` detects this by checking whether the return type's
span start coincides with the function symbol's span start (same line and column).
If so, the return type is not emitted.

### Method Parent Type

For `def Foo::bar(…)`, the parser creates a `parent_type` whose span covers
the full `Foo::bar` text. The formatter uses `parent_type.u.unresolved` (the
inner AST node) which has a span covering only `Foo`, so the type is formatted
correctly without duplicating the method name.

### Multi-Field Struct Declarations

`format_struct` detects consecutive struct fields that share an identical type
span (same start line and column), which is the AST representation of
`a, b, c: Type`. It groups them and emits `a, b, c: Type` rather than separate
lines.

---

## Range Formatting

Range formatting (`--range S:E`) formats only the declarations and statements
that overlap a specified source line range, leaving everything else verbatim.

### Data Structures

```
struct DeclMapping {
    source_start: u32   // first source line of this declaration
    source_end: u32     // last source line of this declaration
    output_start: u32   // first output line (in the full-format output)
    output_end: u32     // last output line
}
```

`decl_mappings` is populated by `format_ns` (one entry per declaration).
`stmt_mappings` is populated by `format_block` when `track_stmts = true`
(one entry per statement, plus a pseudo-entry for the function header).

### Algorithm in `run()`

1. `format_ns(track_mappings: true)` runs a **full** format of the file into
   `output`. This produces both the complete formatted text and the mapping
   tables.
2. `validate_mappings()` (debug mode only) checks that `decl_mappings` are
   monotonic and non-overlapping.
3. `split_lines()` splits both the original source and the formatted output into
   line vectors.
4. `reconstruct_range()` builds the final result by iterating `decl_mappings`:
   - **Non-overlapping declaration**: emit source lines verbatim.
   - **Overlapping declaration with no `stmt_mappings`** (imports, structs, enums,
     arrow functions): apply `emit_mapping_substitution()` treating the whole
     declaration as a single unit.
   - **Overlapping declaration with statements**: call `filter_leaf_mappings()` to
     find the innermost (non-containing) statement mappings within this declaration,
     then for each leaf apply `emit_mapping_substitution()` with source lines
     filling the gaps between statements.

### `emit_mapping_substitution` Cases

Given a single mapping and the requested range `[R_start, R_end]`:

| Condition | Action |
|-----------|--------|
| No overlap with `[R_start, R_end]` | Emit source lines verbatim |
| Fully within `[R_start, R_end]` | Emit formatted output lines |
| Partially overlapping, same line count | Per-line selection (source or formatted per line) |
| Partially overlapping, different line count | Emit formatted if **all** source lines are in range, else keep source |

### `filter_leaf_mappings`

Removes any statement mapping that strictly contains another. This gives the
innermost per-statement granularity, which is what is needed to apply
line-precise substitution.

---

## LSP Integration

The formatter is exposed to editors through the LSP server in the separate
`ocen-vscode` repository (`server/src/server.ts`). The LSP server is a
TypeScript Node.js process using `vscode-languageserver`.

### `runFormatter(text, settings, rangeStartLine?, rangeEndLine?)`

1. Writes the document text to a temp file via `fs.writeFileSync`.
2. Builds the argument list: `['format']`, optionally `['--range', 'S:E']`, and
   optionally `['--line-width', N]` from the `formatterLineWidth` setting.
3. Executes `<compiler> format [args] <tmpfile>` via `exec()` with a configurable
   timeout.
4. Returns `stdout` as the new text. On error, returns whatever stdout remains
   (the formatter itself prints original source on parse failure, so the document
   is never corrupted).

### `minimalDiffEdit(oldText, newText)`

Rather than replacing the entire document, the LSP server finds the smallest
contiguous range that differs between old and new text (by scanning from both
ends) and returns a single `TextEdit` covering only that range. This avoids
unnecessary cursor / scrollbar disruption in the editor.

### Capabilities Registered

```typescript
documentFormattingProvider: true,
documentRangeFormattingProvider: true,
```

- **`onDocumentFormatting`**: calls `runFormatter(text, settings)` (no range),
  applies the minimal diff edit.
- **`onDocumentRangeFormatting`**: converts the LSP 0-based range to 1-based
  line numbers and calls `runFormatter(text, settings, startLine, endLine)`.

### `formatterLineWidth` Setting

Exposed as a user-configurable VS Code setting. When non-zero, the
`--line-width N` flag is passed to every formatter invocation.

---

## Test Infrastructure

### Unit Tests (`tests/format/`)

Each test consists of a pair of files:
- `testname.oc` — the input source, with a directive on line 1
- `testname.expected` — the expected formatter output

**Directives** (in the `///` doc comment on line 1):

| Directive | Meaning |
|-----------|---------|
| `/// format` | Run `ocen format <file>`, compare to `.expected` |
| `/// format-range: S:E` | Run `ocen format --range S:E <file>`, compare to `.expected` |
| `/// format <opts>` | Run `ocen format <opts> <file>`, compare to `.expected` (e.g. `--line-width 80`) |

All three result types also automatically run an **idempotency check**: the formatter
is run again on the `.expected` file with the same flags, and the output must
equal `.expected`. This catches cases where the formatter's output is not yet in
canonical form.

Tests are run by `meta/test.py` (the same harness used for all compiler tests).

There are currently 69 format test `.oc` files covering:
- Basic structure: imports, functions, structs, enums, typedefs
- Comments: standalone, inline, doc, blank-line preservation, alignment
- Statements: if/else/then, while, for, for-each, match, return, defer, assert
- Expressions: binary ops, calls, member access, cast, closures, format strings
- Arrow functions, operator overloads, namespaces, templates
- Vector/map literals, multi-field structs, value enums
- Range formatting at various offsets, including overlapping multiline decls
- Width-aware formatting with `--line-width`

### Codebase Format Tests (`meta/codebase_format_test.py`)

Runs the formatter over the entire codebase (`tests/`, `std/`, `compiler/`) in
four phases:

**Phase 1 — Full-format (idempotency + comment preservation)**
Every `.oc` file is formatted once. The formatter output is checked for:
- Idempotency: formatting the output again produces the same bytes
- Comment preservation: every `//…` comment in the original appears in the output

**Phase 2 — Range spot-checks (idempotency on already-formatted files)**
30 randomly-selected (seeded) files are range-formatted at the first 25%, middle
~17%, and last 25% of lines. Since the files are already formatted, range
formatting must be a no-op (identical output) and all comments must be preserved.

**Phase 3 — Range diff-based checks (non-range lines unchanged)**
The same 30 files are range-formatted from their *original* on-disk content.
Lines before `S` and after `E` in the output must match the original exactly
(prefix and suffix preservation).

**Phase 4 — Line-width tests**
Every file is formatted with `--line-width 80` and `--line-width 120`. Each is
checked for comment preservation and idempotency.

Run the full suite with:
```shell
ocen compiler/main.oc -o ./build/ocen
bash meta/test_all.sh ./build/ocen
```

Or individual phases:
```shell
# Unit tests
python3 meta/test.py -c ./build/ocen tests/

# Codebase tests only
python3 meta/codebase_format_test.py -c ./build/ocen tests std compiler
```

---

## Formatting Rules Summary

See `docs/CODE_STYLE.md` for the authoritative style guide. Key rules enforced:

- **Indentation**: 4 spaces per level (configurable via `--indent`)
- **Operators**: spaces around binary operators; no space for unary, member
  access (`.`), or function call parens
- **Delimiters**: space after `,`, `:` in type positions, `;`; no trailing whitespace
- **Blank lines**: one blank line between top-level declarations, except:
  - No blank between consecutive `import` lines
  - No blank between consecutive `@compiler` directives
  - No blank between consecutive `let`/`const` globals
  - Between arrow functions: blank only if there was a blank in source
- **Implicit return types**: `Void` return types and the synthesised `I32` on
  `main` are not emitted
- **For-each loops**: always reconstructed from the desugared AST as `for x in coll`
- **Empty structs**: kept on one line: `struct Foo {}`
- **VectorLiteral / MapLiteral**: multi-line if multi-line in source

---

## Debugging

Set the environment variable `OCEN_FORMAT_DEBUG_CURSOR=1` to enable verbose
logging of comment cursor regressions (cases where `emit_inline_comment` or
`emit_comments_before` is called with a line number earlier than the cursor's
last position). Regressions are handled correctly but logged to stderr for
analysis. The count is printed at the end of `run()` when non-zero.
