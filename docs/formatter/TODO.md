# Formatter TODO

## Current Status: Phase 3 Complete (Core Implementation)

## Completed
- [x] Comment struct added to tokens.oc
- [x] Lexer modified to save all comments (with `.inc()` fix to avoid text doubling)
- [x] Comments stored on Program
- [x] Parser transfers comments from lexer to Program
- [x] Format subcommand added to main.oc
- [x] Test infrastructure for format tests (meta/test.py)
- [x] 20 format test cases covering all major constructs
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
- [x] Idempotency verification in tests
- [x] All 20 format tests passing
- [x] All existing compiler tests still passing

## Future Work
- [ ] Blank line preservation inside function bodies (between statement groups)
- [ ] Closure expression formatting
- [ ] Format string literal preservation
- [ ] Multi-line expression wrapping (long lines)
- [ ] Trailing comma handling
- [ ] In-place formatting mode (`ocen format -i <file>`)
- [ ] stdin/pipe support
- [ ] Multiple file formatting
- [ ] Integration with editor save hooks
