## Context

This is the compiler and standard library for the `ocen` programming language. It is written in `ocen` itself. It transpiles to C, and then uses a C compiler to produce executables. The syntax is loosely inspired by Rust/Python, but semantically it is essentially C but abstractions at the level of C++ (ie: basic templates, closures, etc).

You should ignore the ./bootstrap/ directory - this is used to build the initial compiler binary using a pre-existing C compiler.

## About the language

Look at ./docs/GETTING_STARTED.md for an overview of the language features and syntax. This is incomplete, notably missing documentation for:
- Closures
- Value enums (rust-style)
- ... many other things

There are comprehensive tests for ALL features in the ./tests/ directory. You can look at those tests to see how various features (including undocumented ones) work. This and the code itself are the best documentation for now.

The compiler implementation is located in ./compiler/, and the stdlib is in ./std/.

## Building

```shell
# Compile a file (using pre-existing compiler)
ocen foo.oc   # default output is ./out (and ./out.c)
ocen foo.oc -o ./foo  # (./foo.c is intermediate C output)
ocen foo.oc -r x y z # compile + run executable with args `x y z`

# Build the compiler from source (using pre-existing compiler)
ocen compiler/main.oc -o ./build/ocen

# Build the compiler, and then use the newly built compiler to run a test
ocen compiler/main.oc -r ./tests/foo.oc

# Build the compiler, and then use the newly built compiler to run all tests (through python harness)
ocen compiler/main.oc -o ./build/ocen
./meta/test.py -c ./build/ocen

# Automatically test building compiler (3-stage bootstrapping) and run all tests. If successful, then it replaces the existing compiler binary with the newly built one, and replaces the bootstrap C code.
./meta/gen_bootstrap.py
```

For more details, see the README.md file and ./docs/COMPILER_DEVELOPENT.md


## LSP

The compiler has built-in support for LSP (Language Server Protocol). This LSP is implemented as part of the compiler binary itself. The implementation is located in ./compiler/lsp/. This is broken into 2 parts:

- `cli`: This is the core, which is responsible for actually finding the symbols, types, etc at given source locations. This is exposed as a CLI tool (and not through the LSP protocol directly). This makes it easy to experiment without an LSP harness, and is also used by the testing infrastructure.
- `server`: This is the actual long-running LSP server which communicates with editors/IDEs. It uses the `cli` tool to get the actual information, and is just responsible for the LSP protocol communication (in particular formatting of the JSON messages, maintaining state between requests, etc).

NOTE that the `cli` tool is called once per request, so it is not optimized for performance. The `server` keeps running and maintains state between requests, so it is more optimized. The `cli` tool can also directly be called from VSCode/other editor LSP servers.

## Testing

All tests are in the ./tests/ directory. Each test is a `.oc` file with special directives in comments at the top of the file. Each test can specify expected exit codes, outputs, error messages, etc. See the existing tests for examples. Every feature should have corresponding positive and negative tests (negative tests are supposed to check for failure cases, and in ./tests/bad/).

Any new feature added should have corresponding tests added, including all possible edge cases, and every single possible error case. We are essentially aiming for 100% code code coverage in tests (although we don't have code coverage tooling yet).

### Running all tests

The easiest way to run everything is the `meta/test_all.sh` script, which runs unit tests, compiles examples, and runs codebase format tests:

```shell
# Build the compiler, then run all tests
ocen compiler/main.oc -o ./build/ocen
bash meta/test_all.sh ./build/ocen
```

### Individual test suites

```shell
# Unit tests only (tests/ directory)
python3 meta/test.py -c ./build/ocen tests/

# Compile examples only
bash meta/compile_examples.sh ./build/ocen

# Codebase format tests (idempotency, comment preservation, range checks)
python3 meta/codebase_format_test.py -c ./build/ocen tests std compiler
```

### Formatter tests

Formatter tests live in `tests/format/`. Each test is a `.oc` file with a `/// format` or `/// format-range: S:E` directive. The expected output is in a corresponding `.expected` file. All format tests (including range) have idempotency checks: running the formatter on the `.expected` file should produce the same output.

The codebase format test (`meta/codebase_format_test.py`) supplements the unit tests by running the formatter on every `.oc` file in the codebase and checking that formatting is idempotent, all comments are preserved, and range formatting is consistent.