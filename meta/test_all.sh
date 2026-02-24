#!/usr/bin/env bash
#
# Run all tests for the ocen compiler:
#   1. Unit tests (tests/)
#   2. Compile examples (examples/)
#   3. Codebase format tests (idempotency + comment preservation + range checks)
#
# Usage:
#   ./meta/test_all.sh              # Use default compiler at ./build/ocen
#   ./meta/test_all.sh ./build/ocen # Specify compiler explicitly
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [ -n "$1" ]; then
    COMPILER="$1"
else
    COMPILER=$(which ocen)
    echo "Using default compiler from PATH: $COMPILER"
fi

if [ ! -f "$COMPILER" ]; then
    echo "Compiler not found at $COMPILER"
    echo "Build it first: ocen compiler/main.oc -o ./build/ocen"
    exit 1
fi

FAILED=0

echo "=========================================="
echo " Running unit tests"
echo "=========================================="
if python3 meta/test.py -c "$COMPILER" tests/; then
    echo ""
else
    echo "Unit tests FAILED"
    FAILED=1
fi

echo ""
echo "=========================================="
echo " Compiling examples"
echo "=========================================="
if bash meta/compile_examples.sh "$COMPILER"; then
    echo "Examples compiled successfully"
else
    echo "Compiling examples FAILED"
    FAILED=1
fi

echo ""
echo "=========================================="
echo " Codebase format tests"
echo "=========================================="
if python3 meta/codebase_format_test.py -c "$COMPILER" tests std compiler; then
    echo ""
else
    echo "Codebase format tests FAILED"
    FAILED=1
fi

echo ""
echo "=========================================="
echo " Running LSP server tests"
echo "=========================================="
if python3 meta/test_lsp_server.py -c "$COMPILER" --parallel; then
    echo ""
else
    echo "LSP server tests FAILED"
    FAILED=1
fi

echo ""
echo "=========================================="
echo " Running DocGen test"
echo "=========================================="
if OCEN="$COMPILER" ./meta/gen_docs.sh /tmp/docs.json; then
    echo ""
else
    echo "DocGen test failed"
    FAILED=1
fi


echo ""
echo "=========================================="
if [ $FAILED -eq 0 ]; then
    echo " All tests PASSED"
else
    echo " Some tests FAILED"
    exit 1
fi
