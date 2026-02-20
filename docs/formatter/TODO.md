# Formatter TODO

## Current Status: Production-ready

The formatter handles all major ocen language constructs, passes 406+ unit tests,
and is verified across the entire codebase (~304 files) via codebase format tests
(idempotency, comment preservation, and range formatting checks).

## Completed
- [x] Comment struct added to tokens.oc
- [x] Lexer modified to save all comments (with `.inc()` fix to avoid text doubling)
- [x] Comments stored on Program
- [x] Parser transfers comments from lexer to Program
- [x] Format subcommand added to main.oc
- [x] Test infrastructure for format tests (meta/test.py)
- [x] 40+ format test cases covering all major constructs
- [x] Top-level structure (collect & sort declarations by span)
- [x] Import statements (including nested/multiple/wildcard)
- [x] Variable/constant declarations (local and global)
- [x] Function definitions (block, arrow, methods, templates)
- [x] Struct/union definitions (including multi-field declarations)
- [x] Enum definitions (including value enums with variants)
- [x] Namespace blocks (nested)
- [x] Typedefs
- [x] Statements (if/else/then, while, for, for-each, match, return, defer, assert, break, continue)
- [x] Expressions (binary, unary, calls, member access, cast, sizeof, specialization, literals)
- [x] Comment interleaving (standalone, inline, doc comments)
- [x] Comment filtering by filename (prevents stdlib comment leakage)
- [x] Source blank line preservation between comments
- [x] Implicit return type detection (Void, main I32)
- [x] Method parent_type span fix (use u.unresolved AST node)
- [x] For-each loop reconstruction from desugared AST
- [x] Operator overload attribute formatting
- [x] Spacing rules (blank lines between declarations, no blanks between imports/vars)
- [x] Idempotency verification in tests (both format and format-range)
- [x] Closure expression formatting
- [x] Format string literal preservation (backtick strings, f-strings, `${}` interpolation)
- [x] Blank line preservation inside function bodies (between statement groups)
- [x] Inline comment alignment (post-processing pass)
- [x] Multi-line VectorLiteral and MapLiteral formatting
- [x] Range formatting (`ocen format --range S:E <file>`)
- [x] Codebase format tests: idempotency, comment preservation, range checks (meta/codebase_format_test.py)

## Testing

```shell
# Unit tests (tests/format/ directory)
python3 meta/test.py -c ./build/ocen tests/

# Codebase format tests (idempotency, comment preservation, range checks)
python3 meta/codebase_format_test.py -c ./build/ocen tests std compiler

# All tests at once
bash meta/test_all.sh ./build/ocen
```

## Future Work
- [ ] Multi-line expression wrapping (long lines)
- [ ] In-place formatting mode (`ocen format -i <file>`)
- [ ] stdin/pipe support
- [ ] Multiple file formatting
- [ ] Integration with editor save hooks
