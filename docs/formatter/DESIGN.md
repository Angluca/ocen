# Ocen Formatter Design

## Overview

The ocen formatter (`ocen format <file.oc>`) is an AST-based code formatter that reads an ocen source file, formats it according to the project's style guide (docs/CODE_STYLE.md), and outputs the result to stdout.

## Key Design Principles

1. **AST-based**: The formatter operates on the parsed AST, not raw tokens
2. **Comment preservation**: ALL comments are preserved in their relative positions
3. **Idempotent**: Running the formatter twice produces identical output
4. **Non-destructive**: Output goes to stdout; the original file is never modified

## Architecture

### Comment Preservation

The lexer was modified to save ALL comments (not just doc comments) into a `Vector<Comment>` stored on the `Program`. Each `Comment` contains:
- `text`: Full comment text including `//` prefix
- `span`: Source location (line, column)
- `is_doc`: Whether it's a doc comment (`///`, `//!`, `//*`, `//.`)
- `is_inline`: Whether there was code before it on the same line

During formatting, a "comment cursor" tracks the current position in the sorted comment list. Before emitting each AST node, any comments with line numbers before the node's start line are emitted.

### Source Order Reconstruction

The `Namespace` stores declarations in separate typed lists (functions, structs, enums, etc.), losing source order. The formatter reconstructs order by collecting all declarations and sorting them by their `span.start` position.

### Hybrid AST + Source Approach

- **AST structure** guides formatting decisions (indentation, spacing rules)
- **Original source text** (via `Program.sources` + spans) is used for faithful reproduction of:
  - String literal delimiters (`"` vs `` ` `` vs `f"`)
  - Numeric literal formats (hex, binary, suffixes)
  - `then` keyword usage
  - Compiler directive text

### Parsing Pipeline

The formatter uses a simplified pipeline:
- Lex the file (with full comment preservation)
- Parse into AST (existing parser)
- Skip typechecking and codegen
- `include_stdlib = false`

## Files

| File | Purpose |
|------|---------|
| `compiler/formatter.oc` | Main formatter implementation |
| `compiler/main.oc` | Modified: format subcommand dispatch |
| `compiler/lexer.oc` | Modified: saves all comments |
| `compiler/tokens.oc` | Modified: Comment struct added |
| `compiler/ast/program.oc` | Modified: comments field on Program |
| `compiler/parser.oc` | Modified: transfers comments from lexer to Program |

## Formatting Rules

See `docs/CODE_STYLE.md` for the full style guide. Key rules:
- 4-space indentation (configurable via `--indent`)
- Spaces around binary operators
- Space after commas, colons, semicolons
- No spaces for unary ops, member access, function calls
- No trailing whitespace
- Blank line between top-level declarations (except between consecutive imports, or consecutive let/const)
- Source blank lines between comments are preserved
- Implicit return types (Void, or I32 for main) are not emitted

## Implementation Details

### Return Type Detection
The parser synthesizes return types for functions without explicit annotations:
- `def main()` gets `I32` with span pointing at `main`
- `def foo()` gets `Void`
The formatter detects this by checking if the return type source text equals the function name, or if the base type is `Void`.

### Method Parent Type
For methods like `def Foo::method(this)`, the parser creates a `parent_type` whose span covers the full `Foo::method`. The formatter uses `parent_type.u.unresolved` (the inner AST node) which has the correct span covering only `Foo`.

### For-Each Loop Reconstruction
The parser desugars `for x in collection { ... }` into a C-style for loop with a generated `_iN` variable. The formatter detects this pattern by checking if the init variable name starts with `_i`, then reconstructs the original syntax from the body structure.

### Comment Filtering
Comments from all parsed files (including transitively loaded ones) are stored in `program.comments`. The formatter filters to only include comments from the current file being formatted, using `Location.filename` comparison.
