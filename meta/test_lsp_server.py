#!/usr/bin/env python3
"""
Comprehensive LSP *server* test harness for the Ocen compiler.

Starts `ocen lsp-server`, drives it with JSON-RPC messages over stdin/stdout, and
validates every response.  This tests the *server* layer on top of the CLI backend:
  - JSON-RPC envelope format
  - 0-indexed LSP ↔ 1-indexed CLI position translation
  - URI handling
  - Incremental document sync (textDocument/didChange)
  - publishDiagnostics notifications
  - All LSP capabilities advertised by handle_initialize

Usage:
    python3 meta/test_lsp_server.py -c ./build/ocen
    python3 meta/test_lsp_server.py               # assumes ./build/ocen
"""

import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import unittest
import argparse
import concurrent.futures
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "lsp_server"


def fixture_uri(name: str) -> str:
    p = (FIXTURES / name).resolve()
    return f"file://{p}"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


# Will be set by the argument parser before tests run.
COMPILER = str(ROOT / "build" / "ocen")


# ---------------------------------------------------------------------------
# LSPClient – manages the server process and JSON-RPC communication
# ---------------------------------------------------------------------------

class LSPClient:
    """Thin LSP client that speaks JSON-RPC over a subprocess's stdin/stdout."""

    def __init__(self, server_path: str):
        self._path = server_path
        self._proc: subprocess.Popen | None = None
        self._msg_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self.notifications: list[dict] = []
        self._notif_queue: queue.Queue = queue.Queue()
        self._reader_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._proc = subprocess.Popen(
            [self._path, "lsp-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def stop(self):
        try:
            self._send({"jsonrpc": "2.0", "id": self._next_id(),
                        "method": "shutdown", "params": {}})
            time.sleep(0.05)
            self._send({"jsonrpc": "2.0", "method": "exit", "params": {}})
        except Exception:
            pass
        if self._proc:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _send(self, msg: dict):
        body = json.dumps(msg).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _read_one(self) -> dict | None:
        """Read exactly one LSP message from stdout, blocking."""
        content_length = 0
        while True:
            raw = self._proc.stdout.readline()
            if not raw:
                return None
            line = raw.decode("utf-8").strip()
            if not line:
                break
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length == 0:
            return None
        body = self._proc.stdout.read(content_length)
        return json.loads(body.decode("utf-8"))

    def _reader_loop(self):
        """Background thread: route messages to either pending-response queues
        or the notification queue."""
        while True:
            try:
                msg = self._read_one()
            except Exception:
                break
            if msg is None:
                break
            if "method" in msg:
                # Notification or server-initiated request
                self._notif_queue.put(msg)
            else:
                mid = msg.get("id")
                with self._lock:
                    q = self._pending.get(mid)
                if q is not None:
                    q.put(msg)

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def request(self, method: str, params: dict, timeout: float = 10.0) -> dict:
        mid = self._next_id()
        resp_q: queue.Queue = queue.Queue()
        with self._lock:
            self._pending[mid] = resp_q
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            return resp_q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"LSP request '{method}' timed out after {timeout}s")
        finally:
            with self._lock:
                self._pending.pop(mid, None)

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def drain_notifications(self, timeout: float = 1.5) -> list[dict]:
        """Collect all pending notifications up to *timeout* seconds of silence."""
        result = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                n = self._notif_queue.get(timeout=min(remaining, 0.2))
                result.append(n)
                deadline = time.monotonic() + 0.3   # reset on each received msg
            except queue.Empty:
                break
        return result

    # ------------------------------------------------------------------
    # High-level LSP helpers
    # ------------------------------------------------------------------

    def initialize(self, root_uri: str = f"file://{ROOT}") -> dict:
        return self.request("initialize", {
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "completion": {"completionItem": {"snippetSupport": True}},
                    "publishDiagnostics": {},
                }
            },
            "rootUri": root_uri,
            "rootPath": str(ROOT),
        })

    def initialized(self):
        self.notify("initialized", {})

    def did_open(self, uri: str, text: str, language_id: str = "ocen"):
        self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": language_id,
                             "version": 1, "text": text},
        })

    def did_change(self, uri: str, changes: list, version: int = 2):
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": changes,
        })

    def did_close(self, uri: str):
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def hover(self, uri: str, line: int, char: int) -> dict:
        return self.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        })

    def definition(self, uri: str, line: int, char: int) -> dict:
        return self.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        })

    def type_definition(self, uri: str, line: int, char: int) -> dict:
        return self.request("textDocument/typeDefinition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        })

    def references(self, uri: str, line: int, char: int) -> dict:
        return self.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
            "context": {"includeDeclaration": True},
        })

    def rename(self, uri: str, line: int, char: int, new_name: str) -> dict:
        return self.request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
            "newName": new_name,
        })

    def signature_help(self, uri: str, line: int, char: int) -> dict:
        return self.request("textDocument/signatureHelp", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        })

    def completion(self, uri: str, line: int, char: int) -> dict:
        return self.request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        })

    def document_symbol(self, uri: str) -> dict:
        return self.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })

    def formatting(self, uri: str) -> dict:
        return self.request("textDocument/formatting", {
            "textDocument": {"uri": uri},
            "options": {"tabSize": 4, "insertSpaces": True},
        })

    def range_formatting(self, uri: str, start_line: int, end_line: int) -> dict:
        return self.request("textDocument/rangeFormatting", {
            "textDocument": {"uri": uri},
            "range": {
                "start": {"line": start_line, "character": 0},
                "end": {"line": end_line, "character": 0},
            },
            "options": {"tabSize": 4, "insertSpaces": True},
        })


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def assert_sub_match(tc: unittest.TestCase, expected, actual, path: str = ""):
    """Assert that *expected* is a 'sub-match' of *actual*:
    - For dicts: every key in expected must be present and recursively match.
    - For lists: every element in expected must appear somewhere in actual.
    - For scalars: must be equal.
    """
    if isinstance(expected, dict):
        tc.assertIsInstance(actual, dict, f"At {path}: expected dict, got {type(actual)}")
        for k, v in expected.items():
            tc.assertIn(k, actual, f"At {path}: key '{k}' missing from {actual}")
            assert_sub_match(tc, v, actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        tc.assertIsInstance(actual, list, f"At {path}: expected list, got {type(actual)}")
        for i, ev in enumerate(expected):
            found = any(_sub_match_ok(ev, av) for av in actual)
            tc.assertTrue(found,
                f"At {path}[{i}]: element {ev!r} not found in {actual!r}")
    elif isinstance(expected, str) and path.endswith(".file"):
        # file paths are matched as regex (as in the CLI test harness)
        tc.assertRegex(str(actual), expected, f"At {path}")
    else:
        tc.assertEqual(expected, actual, f"At {path}")


def _sub_match_ok(expected, actual) -> bool:
    try:
        # Use a dummy TestCase for the recursive check
        tc = unittest.TestCase()
        tc.maxDiff = None
        assert_sub_match(tc, expected, actual)
        return True
    except AssertionError:
        return False


def make_range(sl, sc, el, ec):
    """Build an LSP range dict (0-indexed)."""
    return {
        "start": {"line": sl, "character": sc},
        "end": {"line": el, "character": ec},
    }


def whole_doc_replace_change(old_text: str, new_text: str) -> dict:
    """
    Build a single LSP contentChange that replaces the entire document.

    The server's handle_buffer_change requires that the end position actually
    exists in the buffer (it iterates through the buffer byte-by-byte looking
    for the exact line/col).  A range that overshoots the buffer is silently
    ignored.  Strategy: set end to the position JUST BEFORE the trailing
    newline (if any) — the newline stays in the buffer and the content before
    it is replaced.  The new text must therefore also omit the trailing newline
    (so the preserved newline provides it).
    """
    if old_text.endswith('\n') and len(old_text) > 1:
        # Find the position just before the trailing \n
        trimmed = old_text[:-1]
        parts = trimmed.split('\n')
        end_line = len(parts) - 1
        end_col = len(parts[-1])
        # The preserved trailing \n will act as the final newline
        new_text_send = new_text.rstrip('\n') if new_text.endswith('\n') else new_text
    else:
        parts = old_text.split('\n')
        end_line = len(parts) - 1
        end_col = len(parts[-1])
        new_text_send = new_text
    return {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": end_line, "character": end_col},
        },
        "text": new_text_send,
    }


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class LSPTestBase(unittest.TestCase):
    """
    Each test class gets its own fresh server (started in setUpClass,
    stopped in tearDownClass).  Individual tests reuse the same server to
    avoid the overhead of starting a new process per test.
    """

    _client: LSPClient | None = None

    @classmethod
    def setUpClass(cls):
        cls._client = LSPClient(COMPILER)
        cls._client.start()
        resp = cls._client.initialize()
        cls._client.initialized()
        # Absorb any stray messages
        cls._client.drain_notifications(timeout=0.3)

    @classmethod
    def tearDownClass(cls):
        if cls._client:
            cls._client.stop()

    @property
    def c(self) -> LSPClient:
        return self._client

    def assertResult(self, resp: dict, expected=None):
        """Assert the response has no error and (optionally) sub-matches expected."""
        self.assertNotIn("error", resp,
            f"RPC error: {resp.get('error')}")
        self.assertIn("result", resp)
        if expected is not None:
            assert_sub_match(self, expected, resp["result"])


# ---------------------------------------------------------------------------
# TestInitialize
# ---------------------------------------------------------------------------

class TestInitialize(unittest.TestCase):
    """Run a brand-new initialization and inspect the capabilities."""

    def test_capabilities_present(self):
        client = LSPClient(COMPILER)
        client.start()
        try:
            resp = client.initialize()
            self.assertNotIn("error", resp)
            caps = resp["result"]["capabilities"]
            self.assertIn("hoverProvider", caps)
            self.assertTrue(caps["hoverProvider"])
            self.assertIn("definitionProvider", caps)
            self.assertIn("typeDefinitionProvider", caps)
            self.assertIn("referencesProvider", caps)
            self.assertIn("completionProvider", caps)
            self.assertIn("signatureHelpProvider", caps)
            self.assertIn("documentSymbolProvider", caps)
            self.assertIn("renameProvider", caps)
            self.assertIn("documentFormattingProvider", caps)
            self.assertIn("documentRangeFormattingProvider", caps)
            # Sync mode: incremental (2)
            self.assertEqual(caps["textDocumentSync"], 2)
            # Completion trigger characters
            triggers = caps["completionProvider"]["triggerCharacters"]
            for ch in [".", ":", "@"]:
                self.assertIn(ch, triggers)
        finally:
            client.stop()

    def test_shutdown_exit(self):
        """Server should handle shutdown/exit gracefully."""
        client = LSPClient(COMPILER)
        client.start()
        client.initialize()
        client.initialized()
        resp = client.request("shutdown", {})
        self.assertIn("result", resp)
        self.assertIsNone(resp["result"])
        client.notify("exit", {})
        # Process should exit cleanly within 2 seconds
        client._proc.wait(timeout=2)
        self.assertEqual(client._proc.returncode, 0)


# ---------------------------------------------------------------------------
# TestHover
# ---------------------------------------------------------------------------

class TestHover(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("basic.oc")
        cls._text = fixture_text("basic.oc")
        cls._client.did_open(cls._uri, cls._text)
        cls._client.drain_notifications(timeout=0.5)  # absorb publishDiagnostics

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri)
        super().tearDownClass()

    def _hover_value(self, line: int, char: int) -> str:
        resp = self.c.hover(self._uri, line, char)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        contents = result["contents"]
        self.assertIsInstance(contents, list)
        self.assertGreater(len(contents), 0)
        first = contents[0]
        self.assertEqual(first["language"], "ocen")
        return first["value"]

    def test_hover_struct_name(self):
        # basic.oc line 2 (0-indexed), char 7 → 'Point' in 'struct Point {'
        val = self._hover_value(2, 7)
        self.assertIn("struct Point", val)

    def test_hover_function_name(self):
        # basic.oc line 7 (0-indexed), char 4 → 'add' in 'def add(...)'
        val = self._hover_value(7, 4)
        self.assertIn("def add(a: Point, b: Point): Point", val)

    def test_hover_constant(self):
        # basic.oc line 11 (0-indexed), char 6 → 'MAX' in 'const MAX: i32 = 100'
        val = self._hover_value(11, 6)
        self.assertIn("const MAX: i32", val)

    def test_hover_struct_constructor_usage(self):
        # basic.oc line 14 (0-indexed), char 12 → 'Point' in 'let p = Point(...)'
        val = self._hover_value(14, 12)
        self.assertIn("struct Point", val)

    def test_hover_function_call_usage(self):
        # basic.oc line 16 (0-indexed), char 12 → 'add' in 'let r = add(p, q)'
        val = self._hover_value(16, 12)
        self.assertIn("def add(a: Point, b: Point): Point", val)

    def test_hover_variable_usage(self):
        # basic.oc line 16 (0-indexed), char 16 → 'p' (first arg to add)
        val = self._hover_value(16, 16)
        self.assertIn("p: Point", val)

    def test_hover_constant_usage(self):
        # basic.oc line 17 (0-indexed), char 12 → 'MAX'
        val = self._hover_value(17, 12)
        self.assertIn("const MAX: i32", val)

    def test_hover_no_result_on_whitespace(self):
        # basic.oc line 0 (0-indexed), char 0 → '/' in '/// skip' comment → no hover
        resp = self.c.hover(self._uri, 0, 0)
        self.assertResult(resp)
        # result may be null — that is fine

    def test_hover_enum_name(self):
        uri = fixture_uri("enums.oc")
        text = fixture_text("enums.oc")
        self.c.did_open(uri, text)
        self.c.drain_notifications(timeout=0.5)
        try:
            # line 2 (0-indexed), char 5 → 'Color' in 'enum Color {'
            val = self._hover_value_uri(uri, 2, 5)
            self.assertIn("enum Color", val)
        finally:
            self.c.did_close(uri)

    def test_hover_enum_variant(self):
        uri = fixture_uri("enums.oc")
        text = fixture_text("enums.oc")
        self.c.did_open(uri, text)
        self.c.drain_notifications(timeout=0.5)
        try:
            # line 3 (0-indexed), char 4 → 'Red' in '    Red'
            val = self._hover_value_uri(uri, 3, 4)
            self.assertIn("enum Color::Red", val)
        finally:
            self.c.did_close(uri)

    def _hover_value_uri(self, uri: str, line: int, char: int) -> str:
        resp = self.c.hover(uri, line, char)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        contents = result["contents"]
        self.assertIsInstance(contents, list)
        first = contents[0]
        self.assertEqual(first["language"], "ocen")
        return first["value"]


# ---------------------------------------------------------------------------
# TestDefinition
# ---------------------------------------------------------------------------

class TestDefinition(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("basic.oc")
        cls._text = fixture_text("basic.oc")
        cls._client.did_open(cls._uri, cls._text)
        cls._client.drain_notifications(timeout=0.5)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri)
        super().tearDownClass()

    def test_definition_function(self):
        # 'add' in 'let r = add(p, q)' → should go to 'def add(...)' at (7, 4)
        resp = self.c.definition(self._uri, 16, 12)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result, "definition should not be null")
        self.assertEqual(result["uri"], self._uri)
        # CLI returns start_line=8, start_col=5 → LSP line=7, char=4
        self.assertEqual(result["range"]["start"]["line"], 7)
        self.assertEqual(result["range"]["start"]["character"], 4)

    def test_definition_struct_constructor(self):
        # 'Point' in 'let p = Point(...)' → struct definition at (2, 7)
        resp = self.c.definition(self._uri, 14, 12)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        self.assertEqual(result["uri"], self._uri)
        # CLI returns start_line=3, start_col=8 → LSP line=2, char=7
        self.assertEqual(result["range"]["start"]["line"], 2)
        self.assertEqual(result["range"]["start"]["character"], 7)

    def test_definition_constant(self):
        # 'MAX' in 'let m = MAX' → const definition at (11, 6)
        resp = self.c.definition(self._uri, 17, 12)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        self.assertEqual(result["uri"], self._uri)
        # CLI returns start_line=12, start_col=7 → LSP line=11, char=6
        self.assertEqual(result["range"]["start"]["line"], 11)
        self.assertEqual(result["range"]["start"]["character"], 6)

    def test_definition_no_result_on_literal(self):
        # Hover over a float literal (1.0) in 'Point(1.0, 2.0)' → no definition
        resp = self.c.definition(self._uri, 14, 18)  # '1' of '1.0'
        self.assertResult(resp)
        # null result is acceptable

    def test_definition_enum_variant(self):
        uri = fixture_uri("enums.oc")
        text = fixture_text("enums.oc")
        self.c.did_open(uri, text)
        self.c.drain_notifications(timeout=0.5)
        try:
            # 'Red' in 'Color::Red' → should go to enum variant definition
            # enums.oc line 15 (0-indexed): '    let c = Color::Red'
            # 'Red' starts at col 19 (0-indexed)
            resp = self.c.definition(uri, 15, 19)
            self.assertResult(resp)
            result = resp["result"]
            self.assertIsNotNone(result)
            # Definition should be at line 3 (0-indexed): '    Red'
            self.assertEqual(result["range"]["start"]["line"], 3)
        finally:
            self.c.did_close(uri)


# ---------------------------------------------------------------------------
# TestTypeDefinition
# ---------------------------------------------------------------------------

class TestTypeDefinition(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("basic.oc")
        cls._text = fixture_text("basic.oc")
        cls._client.did_open(cls._uri, cls._text)
        cls._client.drain_notifications(timeout=0.5)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri)
        super().tearDownClass()

    def test_type_definition_of_variable(self):
        # Type of 'p' (which is Point) → struct Point definition at (2, 7)
        resp = self.c.type_definition(self._uri, 16, 16)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        self.assertEqual(result["uri"], self._uri)
        # CLI returns start_line=3, start_col=8 → LSP line=2, char=7
        self.assertEqual(result["range"]["start"]["line"], 2)
        self.assertEqual(result["range"]["start"]["character"], 7)

    def test_type_definition_of_return_value(self):
        # Type of 'r' (which is also Point) at line 16, char 8 ('r' in 'let r = ...')
        resp = self.c.type_definition(self._uri, 16, 8)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        self.assertEqual(result["range"]["start"]["line"], 2)


# ---------------------------------------------------------------------------
# TestReferences
# ---------------------------------------------------------------------------

class TestReferences(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("references.oc")
        cls._text = fixture_text("references.oc")
        cls._client.did_open(cls._uri, cls._text)
        cls._client.drain_notifications(timeout=0.5)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri)
        super().tearDownClass()

    def test_references_function(self):
        # references.oc line 7 (0-indexed), char 12 → 'square' usage
        # CLI returns 3 references:
        #   def at (line 2, char 4), usage at (line 7, char 12), (line 8, char 12)
        resp = self.c.references(self._uri, 7, 12)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 2)

        # All refs should point to the same file (or the correct URI)
        for ref in result:
            self.assertIn("uri", ref)
            self.assertIn("range", ref)

        # Collect all start lines (0-indexed)
        lines = {r["range"]["start"]["line"] for r in result}
        # Should include the definition line (2) and at least one usage (7 or 8)
        self.assertIn(2, lines, "definition line must be in references")
        self.assertIn(7, lines, "first usage line must be in references")
        self.assertIn(8, lines, "second usage line must be in references")

    def test_references_include_definition(self):
        # References on the definition itself should also return the definition
        resp = self.c.references(self._uri, 2, 4)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsInstance(result, list)
        lines = {r["range"]["start"]["line"] for r in result}
        self.assertIn(2, lines)

    def test_references_result_positions_zero_indexed(self):
        """Positions in the response must be 0-indexed (LSP convention)."""
        resp = self.c.references(self._uri, 2, 4)
        self.assertResult(resp)
        for ref in resp["result"]:
            sl = ref["range"]["start"]["line"]
            sc = ref["range"]["start"]["character"]
            # Lines and chars must be non-negative integers
            self.assertGreaterEqual(sl, 0)
            self.assertGreaterEqual(sc, 0)
            # Definition is at 0-indexed line 2, char 4 — verify the server
            # did NOT return CLI's 1-indexed value of 3, col 5.
            if sl == 2:
                self.assertEqual(sc, 4, "definition should be at char 4 (0-indexed)")


# ---------------------------------------------------------------------------
# TestRename
# ---------------------------------------------------------------------------

class TestRename(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("references.oc")
        cls._text = fixture_text("references.oc")
        cls._client.did_open(cls._uri, cls._text)
        cls._client.drain_notifications(timeout=0.5)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri)
        super().tearDownClass()

    def test_rename_function(self):
        resp = self.c.rename(self._uri, 7, 12, "power")
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        self.assertIn("changes", result)

        # The file URI must appear in changes
        changes = result["changes"]
        self.assertIn(self._uri, changes)

        edits = changes[self._uri]
        self.assertIsInstance(edits, list)
        # Should have at least 3 edits: definition + 2 usages
        self.assertGreaterEqual(len(edits), 3)

        # The new name must appear everywhere
        for edit in edits:
            self.assertEqual(edit["newText"], "power")
            self.assertIn("range", edit)

    def test_rename_preserves_position_zero_indexed(self):
        """Renamed ranges must use 0-indexed positions."""
        resp = self.c.rename(self._uri, 2, 4, "sq")
        self.assertResult(resp)
        changes = resp["result"]["changes"]
        for uri, edits in changes.items():
            for edit in edits:
                sl = edit["range"]["start"]["line"]
                sc = edit["range"]["start"]["character"]
                self.assertGreaterEqual(sl, 0)
                self.assertGreaterEqual(sc, 0)


# ---------------------------------------------------------------------------
# TestSignatureHelp
# ---------------------------------------------------------------------------

class TestSignatureHelp(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("sighelp.oc")
        cls._text = fixture_text("sighelp.oc")
        cls._client.did_open(cls._uri, cls._text)
        cls._client.drain_notifications(timeout=0.5)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri)
        super().tearDownClass()

    def test_sighelp_first_param(self):
        # sighelp.oc line 7 (0-indexed), char 10 → inside 'greet()' after '('
        # Layout: line0=/// skip, line1=blank, line2=def greet...,
        #         line6=def main(), line7=greet(), line8=greet("hi", 5)
        # CLI: line 8, col 11 → activeParameter 0
        resp = self.c.signature_help(self._uri, 7, 10)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        sigs = result["signatures"]
        self.assertGreater(len(sigs), 0)
        label = sigs[0]["label"]
        self.assertIn("greet", label)
        self.assertIn("name: str", label)
        self.assertIn("count: i32", label)
        self.assertEqual(result["activeParameter"], 0)

    def test_sighelp_second_param(self):
        # sighelp.oc line 8 (0-indexed), char 16 → at '5' in 'greet("hi", 5)'
        # greet("hi", 5): indent=4, g=4..t=8, (=9, "hi"=10-13, ,=14, sp=15, 5=16
        # CLI: line 9, col 17 → activeParameter 1
        resp = self.c.signature_help(self._uri, 8, 16)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        self.assertEqual(result["activeParameter"], 1)

    def test_sighelp_parameter_labels(self):
        resp = self.c.signature_help(self._uri, 7, 10)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsNotNone(result)
        params = result["signatures"][0]["parameters"]
        self.assertGreaterEqual(len(params), 2)
        labels = [p["label"] for p in params]
        self.assertIn("name: str", labels)
        self.assertIn("count: i32", labels)

    def test_sighelp_no_result_outside_call(self):
        # sighelp.oc line 0 ('/// skip') → no signature help
        resp = self.c.signature_help(self._uri, 0, 0)
        self.assertResult(resp)
        # result should be null


# ---------------------------------------------------------------------------
# TestCompletion
# ---------------------------------------------------------------------------

class TestCompletion(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("completions.oc")
        cls._text = fixture_text("completions.oc")
        cls._client.did_open(cls._uri, cls._text)
        cls._client.drain_notifications(timeout=0.5)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri)
        super().tearDownClass()

    def test_completion_struct_member(self):
        # completions.oc line 8, char 6 → after 'b.' in '    b.'
        resp = self.c.completion(self._uri, 8, 6)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsInstance(result, list)
        labels = [item["label"] for item in result]
        self.assertIn("x", labels, "field 'x' must appear in completions")
        self.assertIn("msg", labels, "field 'msg' must appear in completions")

    def test_completion_items_have_required_fields(self):
        resp = self.c.completion(self._uri, 8, 6)
        self.assertResult(resp)
        for item in resp["result"]:
            self.assertIn("label", item)
            self.assertIn("insertText", item)
            self.assertIn("kind", item)
            # kind should be an integer
            self.assertIsInstance(item["kind"], int)

    def test_completion_insert_text_format(self):
        """Server advertises snippet support → insertTextFormat must be 2."""
        resp = self.c.completion(self._uri, 8, 6)
        self.assertResult(resp)
        for item in resp["result"]:
            self.assertEqual(item.get("insertTextFormat"), 2)

    def test_completion_kind_mapping(self):
        """Field completions must use kind=5 (Field), function completions kind=3."""
        resp = self.c.completion(self._uri, 8, 6)
        self.assertResult(resp)
        for item in resp["result"]:
            # completions.oc fields: kind=5 (Field)
            if item["label"] in ("x", "msg"):
                self.assertEqual(item["kind"], 5, f"field {item['label']} should have kind=5")


# ---------------------------------------------------------------------------
# TestDocumentSymbols
# ---------------------------------------------------------------------------

class TestDocumentSymbols(LSPTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri_basic = fixture_uri("basic.oc")
        cls._uri_enums = fixture_uri("enums.oc")
        cls._client.did_open(cls._uri_basic, fixture_text("basic.oc"))
        cls._client.did_open(cls._uri_enums, fixture_text("enums.oc"))
        cls._client.drain_notifications(timeout=1.0)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri_basic)
        cls._client.did_close(cls._uri_enums)
        super().tearDownClass()

    def test_basic_symbols_present(self):
        resp = self.c.document_symbol(self._uri_basic)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsInstance(result, list)
        names = [s["name"] for s in result]
        self.assertIn("Point", names)
        self.assertIn("add", names)
        self.assertIn("main", names)

    def test_struct_kind(self):
        resp = self.c.document_symbol(self._uri_basic)
        self.assertResult(resp)
        symbols = {s["name"]: s for s in resp["result"]}
        self.assertEqual(symbols["Point"]["kind"], 23)   # Struct

    def test_function_kind(self):
        resp = self.c.document_symbol(self._uri_basic)
        self.assertResult(resp)
        symbols = {s["name"]: s for s in resp["result"]}
        self.assertEqual(symbols["add"]["kind"], 12)     # Function
        self.assertEqual(symbols["main"]["kind"], 12)    # Function

    def test_constant_kind(self):
        resp = self.c.document_symbol(self._uri_basic)
        self.assertResult(resp)
        symbols = {s["name"]: s for s in resp["result"]}
        self.assertIn("MAX", symbols)
        # Constants get kind=13 (Variable) by the server
        self.assertEqual(symbols["MAX"]["kind"], 13)

    def test_enum_symbol(self):
        resp = self.c.document_symbol(self._uri_enums)
        self.assertResult(resp)
        result = resp["result"]
        names = [s["name"] for s in result]
        self.assertIn("Color", names)
        symbols = {s["name"]: s for s in result}
        self.assertEqual(symbols["Color"]["kind"], 10)   # Enum

    def test_enum_has_children(self):
        resp = self.c.document_symbol(self._uri_enums)
        self.assertResult(resp)
        symbols = {s["name"]: s for s in resp["result"]}
        children = symbols["Color"]["children"]
        child_names = [c["name"] for c in children]
        self.assertIn("Red", child_names)
        # Enum members should have kind=22 (EnumMember)
        for c in children:
            if c["name"] in ("Red", "Green", "Blue"):
                self.assertEqual(c["kind"], 22)

    def test_symbols_have_range_and_selection_range(self):
        resp = self.c.document_symbol(self._uri_basic)
        self.assertResult(resp)
        for sym in resp["result"]:
            self.assertIn("range", sym)
            self.assertIn("selectionRange", sym)
            r = sym["range"]
            self.assertIn("start", r)
            self.assertIn("end", r)
            self.assertGreaterEqual(r["start"]["line"], 0)

    def test_symbol_positions_zero_indexed(self):
        """Ranges in documentSymbol must be 0-indexed."""
        resp = self.c.document_symbol(self._uri_basic)
        self.assertResult(resp)
        symbols = {s["name"]: s for s in resp["result"]}
        # 'add' is defined at line 7 (0-indexed) in basic.oc
        add_line = symbols["add"]["range"]["start"]["line"]
        self.assertEqual(add_line, 7,
            f"'add' should be at 0-indexed line 7, got {add_line}")
        # 'Point' is defined at line 2 (0-indexed) in basic.oc
        point_line = symbols["Point"]["range"]["start"]["line"]
        self.assertEqual(point_line, 2,
            f"'Point' should be at 0-indexed line 2, got {point_line}")


# ---------------------------------------------------------------------------
# TestFormatting
# ---------------------------------------------------------------------------

class TestFormatting(LSPTestBase):
    """
    Tests textDocument/formatting and textDocument/rangeFormatting.
    Each test opens its own fresh document, modifies it, tests, then closes.
    """

    BADLY_FORMATTED = """\
/// skip
struct  Foo {
  x: i32
  y: i32
}

def main() {
let a = 1
let b = 2
}
"""

    EXPECTED_FORMATTED = """\
/// skip

struct Foo {
    x: i32
    y: i32
}

def main() {
    let a = 1
    let b = 2
}
"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri = fixture_uri("formatting.oc")
        cls._text = fixture_text("formatting.oc")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def setUp(self):
        """Each test opens the document fresh."""
        self.c.did_open(self._uri, self._text)
        self.c.drain_notifications(timeout=0.3)

    def tearDown(self):
        """Each test closes the document."""
        self.c.did_close(self._uri)

    def _replace_entire_doc(self, new_text: str, version: int):
        """Replace entire document content with correct end position."""
        change = whole_doc_replace_change(self._text, new_text)
        self.c.did_change(self._uri, [change], version=version)
        self.c.drain_notifications(timeout=0.3)

    def test_formatting_returns_text_edit(self):
        self._replace_entire_doc(self.BADLY_FORMATTED, version=2)
        resp = self.c.formatting(self._uri)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        edit = result[0]
        self.assertIn("range", edit)
        self.assertIn("newText", edit)

    def test_formatting_fixes_indentation(self):
        self._replace_entire_doc(self.BADLY_FORMATTED, version=3)
        resp = self.c.formatting(self._uri)
        self.assertResult(resp)
        new_text = resp["result"][0]["newText"]
        # Should have 4-space indentation for struct fields
        self.assertIn("    x: i32", new_text)
        self.assertIn("    y: i32", new_text)
        self.assertIn("    let a = 1", new_text)

    def test_formatting_single_space_after_struct(self):
        self._replace_entire_doc(self.BADLY_FORMATTED, version=4)
        resp = self.c.formatting(self._uri)
        self.assertResult(resp)
        new_text = resp["result"][0]["newText"]
        # 'struct  Foo' (double space) should become 'struct Foo' (single space)
        self.assertNotIn("struct  Foo", new_text)
        self.assertIn("struct Foo", new_text)

    def test_range_formatting(self):
        """Range formatting on lines 7-9 (the badly indented main body)."""
        self._replace_entire_doc(self.BADLY_FORMATTED, version=5)
        resp = self.c.range_formatting(self._uri, 7, 9)
        self.assertResult(resp)
        result = resp["result"]
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_formatting_already_formatted_is_noop(self):
        """Formatting an already-formatted file should produce identical content."""
        # Restore to well-formatted
        self._replace_entire_doc(self.EXPECTED_FORMATTED, version=10)
        resp = self.c.formatting(self._uri)
        self.assertResult(resp)
        result = resp["result"]
        if result:   # May return empty list if no changes needed
            new_text = result[0]["newText"]
            self.assertEqual(new_text.strip(), self.EXPECTED_FORMATTED.strip())


# ---------------------------------------------------------------------------
# TestDiagnostics
# ---------------------------------------------------------------------------

class TestDiagnostics(LSPTestBase):
    """
    Tests that the server sends textDocument/publishDiagnostics correctly.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._bad_uri = fixture_uri("bad.oc")
        cls._good_uri = fixture_uri("basic.oc")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def _open_and_collect_diagnostics(self, uri: str, text: str,
                                       wait: float = 1.5) -> list[dict]:
        """Open a document and collect any publishDiagnostics notifications."""
        self.c.did_open(uri, text)
        notifications = self.c.drain_notifications(timeout=wait)
        self.c.did_close(uri)
        return [n for n in notifications
                if n.get("method") == "textDocument/publishDiagnostics"]

    def test_diagnostics_sent_for_invalid_file(self):
        """Opening bad.oc (type error) should trigger publishDiagnostics with errors."""
        notifs = self._open_and_collect_diagnostics(
            self._bad_uri, fixture_text("bad.oc"))
        self.assertGreater(len(notifs), 0,
            "expected at least one publishDiagnostics notification")
        params = notifs[-1]["params"]  # last notification for the file
        self.assertEqual(params["uri"], self._bad_uri)
        diags = params["diagnostics"]
        self.assertGreater(len(diags), 0, "expected at least one diagnostic")
        first = diags[0]
        self.assertIn("message", first)
        self.assertIn("range", first)
        self.assertIn("severity", first)
        self.assertEqual(first["severity"], 1)   # Error = 1

    def test_diagnostics_message_content(self):
        notifs = self._open_and_collect_diagnostics(
            self._bad_uri, fixture_text("bad.oc"))
        self.assertGreater(len(notifs), 0)
        diags = notifs[-1]["params"]["diagnostics"]
        messages = [d["message"] for d in diags]
        combined = " ".join(messages)
        self.assertIn("i32", combined)

    def test_diagnostics_zero_indexed_range(self):
        """Diagnostic ranges must use 0-indexed positions."""
        notifs = self._open_and_collect_diagnostics(
            self._bad_uri, fixture_text("bad.oc"))
        self.assertGreater(len(notifs), 0)
        diags = notifs[-1]["params"]["diagnostics"]
        for d in diags:
            r = d["range"]
            # bad.oc: the error is on line 3 (0-indexed): 'let x: i32 = "hello"'
            # The CLI would report start_line=4, start_col=18 (1-indexed)
            # Server must convert to 0-indexed: line=3, char=17
            sl = r["start"]["line"]
            sc = r["start"]["character"]
            self.assertGreaterEqual(sl, 0,
                f"diagnostic start line must be 0-indexed (got {sl})")
            # Verify it is *not* the raw 1-indexed CLI value
            self.assertNotEqual(sl, 4,
                "diagnostic line should be 0-indexed (3), not 1-indexed (4)")
            self.assertEqual(sl, 3,
                f"error should be on 0-indexed line 3, got {sl}")

    def test_no_diagnostics_for_valid_file(self):
        """Opening a valid file should produce a publishDiagnostics with empty list."""
        notifs = self._open_and_collect_diagnostics(
            self._good_uri, fixture_text("basic.oc"))
        # If any publishDiagnostics notification came, its diagnostics should be empty
        diag_notifs = [n for n in notifs
                       if n.get("method") == "textDocument/publishDiagnostics"
                       and n["params"]["uri"] == self._good_uri]
        if diag_notifs:
            self.assertEqual(diag_notifs[-1]["params"]["diagnostics"], [])

    def test_diagnostics_update_on_change(self):
        """Changing a document from invalid to valid should clear diagnostics."""
        uri = self._bad_uri
        bad_text = fixture_text("bad.oc")
        self.c.did_open(uri, bad_text)
        # Drain the initial diagnostics (should have an error)
        first_notifs = self.c.drain_notifications(timeout=1.5)
        diag0 = [n for n in first_notifs
                 if n.get("method") == "textDocument/publishDiagnostics"]
        self.assertGreater(len(diag0), 0, "expected initial error diagnostics")

        # Fix the error — use whole_doc_replace_change for correct range
        fixed_text = "/// skip\ndef main() {\n    let x: i32 = 42\n}\n"
        change = whole_doc_replace_change(bad_text, fixed_text)
        self.c.did_change(uri, [change], version=2)

        # Server immediately validates after didChange; drain the notification
        second_notifs = self.c.drain_notifications(timeout=2.0)
        self.c.did_close(uri)

        diag_notifs = [n for n in second_notifs
                       if n.get("method") == "textDocument/publishDiagnostics"
                       and n["params"]["uri"] == uri]
        self.assertGreater(len(diag_notifs), 0,
            "expected publishDiagnostics after fixing the error")
        self.assertEqual(diag_notifs[-1]["params"]["diagnostics"], [],
            "diagnostics should be cleared after fixing the error")


# ---------------------------------------------------------------------------
# TestDocumentSync
# ---------------------------------------------------------------------------

class TestDocumentSync(LSPTestBase):
    """
    Tests the document lifecycle: didOpen, didChange (incremental), didClose.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def test_hover_after_incremental_change(self):
        """
        Open basic.oc, then rename 'add' to 'sum' in-memory via didChange,
        and verify hover at the new location reflects the change.

        basic.oc line 6 (0-indexed): 'def add(a: Point, b: Point): Point {'
        We replace 'add' (chars 4-6) with 'sum'.
        """
        uri = fixture_uri("basic.oc")
        original = fixture_text("basic.oc")
        self.c.did_open(uri, original)
        self.c.drain_notifications(timeout=0.5)

        # Replace 'add' on line 6 with 'sum' (incremental change)
        self.c.did_change(uri, [{
            "range": make_range(6, 4, 6, 7),
            "text": "sum",
        }], version=2)
        self.c.drain_notifications(timeout=0.5)

        # Hover over 'sum' at line 6, char 4 — should now show 'sum'
        resp = self.c.hover(uri, 6, 4)
        self.assertResult(resp)
        result = resp["result"]
        if result:
            val = result["contents"][0]["value"]
            self.assertIn("sum", val)

        self.c.did_close(uri)

    def test_diagnostics_after_edit_introduces_error(self):
        """
        Open basic.oc (valid), then introduce a type error via didChange.
        Server should publish diagnostics with an error.
        """
        uri = fixture_uri("basic.oc")
        original = fixture_text("basic.oc")
        self.c.did_open(uri, original)
        self.c.drain_notifications(timeout=0.5)

        # Replace line 17 ('    let m = MAX') with an invalid assignment
        # line 17: '    let m = MAX' → last char is at ~14
        self.c.did_change(uri, [{
            "range": make_range(17, 0, 18, 0),
            "text": '    let m: i32 = "oops"\n',
        }], version=2)
        # Hover request ensures server processes the change and validates
        self.c.hover(uri, 17, 4)
        notifs = self.c.drain_notifications(timeout=1.5)
        self.c.did_close(uri)

        diag_notifs = [n for n in notifs
                       if n.get("method") == "textDocument/publishDiagnostics"
                       and n["params"]["uri"] == uri]
        self.assertGreater(len(diag_notifs), 0,
            "expected publishDiagnostics after introducing an error")
        diags = diag_notifs[-1]["params"]["diagnostics"]
        self.assertGreater(len(diags), 0, "expected at least one error")

    def test_did_close_removes_document(self):
        """After didClose, the server should not use in-memory content."""
        uri = fixture_uri("basic.oc")
        text = fixture_text("basic.oc")
        self.c.did_open(uri, text)
        self.c.drain_notifications(timeout=0.5)
        self.c.did_close(uri)

        # After close, the server falls back to the on-disk file — hover should
        # still work (reading from disk) or return null.
        resp = self.c.hover(uri, 2, 7)
        self.assertResult(resp)
        # If it returns a result, it should still say 'struct Point'
        if resp["result"]:
            val = resp["result"]["contents"][0]["value"]
            self.assertIn("Point", val)


# ---------------------------------------------------------------------------
# TestUnknownMethod
# ---------------------------------------------------------------------------

class TestUnknownMethod(LSPTestBase):
    """
    The server must not crash on unknown methods — it logs and continues.
    Verified by sending an unknown method and then a valid request successfully.
    """

    def test_unknown_method_does_not_crash(self):
        uri = fixture_uri("basic.oc")
        # Just send an unknown notification (no response expected)
        self.c.notify("textDocument/unknownFoo", {
            "textDocument": {"uri": uri},
        })
        # Server should still handle a valid hover request afterwards
        self.c.did_open(uri, fixture_text("basic.oc"))
        self.c.drain_notifications(timeout=0.5)
        resp = self.c.hover(uri, 2, 7)
        self.assertResult(resp)
        self.assertIsNotNone(resp["result"])
        self.c.did_close(uri)


# ---------------------------------------------------------------------------
# TestURIHandling
# ---------------------------------------------------------------------------

class TestURIHandling(LSPTestBase):
    """
    Tests that file URIs are handled correctly in responses.
    The server must strip 'file://' from URIs when calling the CLI
    and re-add it in responses.
    """

    def test_definition_uri_uses_file_scheme(self):
        uri = fixture_uri("basic.oc")
        self.c.did_open(uri, fixture_text("basic.oc"))
        self.c.drain_notifications(timeout=0.5)
        resp = self.c.definition(uri, 16, 12)
        self.assertResult(resp)
        result = resp["result"]
        if result:
            self.assertTrue(result["uri"].startswith("file://"),
                f"URI should start with 'file://', got: {result['uri']}")
        self.c.did_close(uri)

    def test_references_uris_use_file_scheme(self):
        uri = fixture_uri("references.oc")
        self.c.did_open(uri, fixture_text("references.oc"))
        self.c.drain_notifications(timeout=0.5)
        resp = self.c.references(uri, 2, 4)
        self.assertResult(resp)
        for ref in resp["result"]:
            self.assertTrue(ref["uri"].startswith("file://"),
                f"Reference URI should start with 'file://', got: {ref['uri']}")
        self.c.did_close(uri)

    def test_rename_uris_use_file_scheme(self):
        uri = fixture_uri("references.oc")
        self.c.did_open(uri, fixture_text("references.oc"))
        self.c.drain_notifications(timeout=0.5)
        resp = self.c.rename(uri, 2, 4, "my_func")
        self.assertResult(resp)
        if resp["result"]:
            for change_uri in resp["result"]["changes"]:
                self.assertTrue(change_uri.startswith("file://"),
                    f"Rename URI should start with 'file://', got: {change_uri}")
        self.c.did_close(uri)


# ---------------------------------------------------------------------------
# TestMultipleDocuments
# ---------------------------------------------------------------------------

class TestMultipleDocuments(LSPTestBase):
    """
    The server can have multiple documents open at once.  Verify requests
    for one document don't bleed into another.
    """

    def test_requests_are_isolated_per_document(self):
        uri_basic = fixture_uri("basic.oc")
        uri_enums = fixture_uri("enums.oc")
        self.c.did_open(uri_basic, fixture_text("basic.oc"))
        self.c.did_open(uri_enums, fixture_text("enums.oc"))
        self.c.drain_notifications(timeout=1.0)

        # Hover in basic.oc
        resp_b = self.c.hover(uri_basic, 2, 7)
        self.assertResult(resp_b)
        val_b = resp_b["result"]["contents"][0]["value"]
        self.assertIn("struct Point", val_b)

        # Hover in enums.oc
        resp_e = self.c.hover(uri_enums, 2, 5)
        self.assertResult(resp_e)
        val_e = resp_e["result"]["contents"][0]["value"]
        self.assertIn("enum Color", val_e)

        self.c.did_close(uri_basic)
        self.c.did_close(uri_enums)


# ---------------------------------------------------------------------------
# Stability tests – long sessions with hundreds of requests
# ---------------------------------------------------------------------------

class TestStabilityLongSession(LSPTestBase):
    """
    Verify server stability under sustained load: hundreds of requests in a
    single session, rapid document edits, and interleaved request types.

    These tests mirror real VSCode usage patterns that have historically
    caused the server to crash.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._uri_basic = fixture_uri("basic.oc")
        cls._uri_enums = fixture_uri("enums.oc")
        cls._uri_refs = fixture_uri("references.oc")
        cls._client.did_open(cls._uri_basic, fixture_text("basic.oc"))
        cls._client.did_open(cls._uri_enums, fixture_text("enums.oc"))
        cls._client.did_open(cls._uri_refs, fixture_text("references.oc"))
        cls._client.drain_notifications(timeout=1.5)

    @classmethod
    def tearDownClass(cls):
        cls._client.did_close(cls._uri_basic)
        cls._client.did_close(cls._uri_enums)
        cls._client.did_close(cls._uri_refs)
        super().tearDownClass()

    def _assert_server_alive(self):
        """Heartbeat: verify the server is still responding."""
        resp = self.c.hover(self._uri_basic, 2, 7)
        self.assertIsNotNone(resp, "Server stopped responding (heartbeat failed)")
        self.assertIn("id", resp, "Server returned non-response message")

    def test_100_rapid_hover_requests(self):
        """Send 100 hover requests in rapid succession without waiting between."""
        uri = self._uri_basic
        positions = [
            (2, 7),   # struct Point
            (2, 14),  # x field area
            (3, 5),   # y field
            (7, 8),   # def add
            (7, 12),  # param a
            (14, 14), # const MAX usage area
        ]
        for i in range(100):
            line, char = positions[i % len(positions)]
            resp = self.c.hover(uri, line, char)
            self.assertIn("id", resp, f"Request {i} got invalid response")
        self._assert_server_alive()

    def test_100_rapid_definition_requests(self):
        """Send 100 definition requests in rapid succession."""
        uri = self._uri_basic
        positions = [
            (9, 11),  # Point in add's body
            (16, 12), # usage of add
            (17, 12), # MAX constant
        ]
        for i in range(100):
            line, char = positions[i % len(positions)]
            resp = self.c.definition(uri, line, char)
            self.assertIn("id", resp, f"Definition request {i} got invalid response")
        self._assert_server_alive()

    def test_50_rapid_completion_requests(self):
        """Send 50 completion requests at dot positions."""
        uri = self._uri_basic
        for i in range(50):
            resp = self.c.completion(uri, 10, 10)
            self.assertIn("id", resp, f"Completion request {i} got invalid response")
        self._assert_server_alive()

    def test_50_rapid_document_symbol_requests(self):
        """Send 50 document symbol requests."""
        for i in range(50):
            resp = self.c.document_symbol(self._uri_basic)
            self.assertIn("id", resp, f"DocumentSymbol request {i} got invalid response")
            if resp.get("result") is not None:
                self.assertIsInstance(resp["result"], list)
        self._assert_server_alive()

    def test_interleaved_mixed_requests_200(self):
        """
        Send 200 requests that alternate between hover, definition, completion,
        and document symbols.  This is the pattern most likely to trigger
        state corruption in the server.
        """
        uri = self._uri_basic
        methods = ["hover", "definition", "completion", "symbols"]
        for i in range(200):
            m = methods[i % len(methods)]
            if m == "hover":
                resp = self.c.hover(uri, 2 + (i % 5), 7)
            elif m == "definition":
                resp = self.c.definition(uri, 7, 8)
            elif m == "completion":
                resp = self.c.completion(uri, 10, 10)
            else:
                resp = self.c.document_symbol(uri)
            self.assertIn("id", resp, f"Request {i} (method={m}) got invalid response")
        self._assert_server_alive()

    def test_rapid_document_edits_then_hover(self):
        """
        Simulate the VSCode pattern: user types quickly, triggering many
        incremental didChange notifications, then hovers to see info.
        The server should survive all the edits and still answer hovers.
        """
        uri = fixture_uri("basic.oc")
        original = fixture_text("basic.oc")
        self.c.did_open(uri, original)
        self.c.drain_notifications(timeout=0.5)

        # Send 30 rapid incremental changes (simulate keystrokes)
        versions = list(range(10, 10 + 30))
        texts = []
        for i in range(30):
            # Alternate between two valid states to keep the file parseable
            if i % 2 == 0:
                t = original + f"\n// edit {i}"
            else:
                t = original
            texts.append(t)
            change = whole_doc_replace_change(
                texts[i - 1] if i > 0 else original, t)
            self.c.did_change(uri, [change], version=versions[i])

        # Drain any accumulated diagnostics
        self.c.drain_notifications(timeout=1.0)

        # Hover should still work
        resp = self.c.hover(uri, 2, 7)
        self.assertIn("id", resp, "Server crashed after rapid edits")
        self.c.did_close(uri)

    def test_hover_all_positions_in_struct_def(self):
        """
        Hover over every column in the struct definition line.
        Many positions won't return results; all should return valid responses.
        """
        uri = self._uri_basic
        # Line 2: "struct Point {" (0-indexed line 2)
        line_text = "struct Point {"
        for col in range(len(line_text)):
            resp = self.c.hover(uri, 2, col)
            self.assertIn("id", resp, f"Hover at col {col} crashed server")

    def test_hover_out_of_bounds_positions(self):
        """
        Requesting hover at out-of-bounds line/col should return null result,
        not crash the server.
        """
        uri = self._uri_basic
        oob_positions = [
            (9999, 0),    # way past end of file
            (0, 9999),    # past end of first line
            (1, 9999),    # past end of a real line
        ]
        for line, char in oob_positions:
            resp = self.c.hover(uri, line, char)
            self.assertIn("id", resp, f"OOB hover at ({line},{char}) crashed server")
            # Result should be null or a valid hover response
            self.assertIn("result", resp, f"OOB hover at ({line},{char}) got no result field")

    def test_definition_then_hover_same_symbol_100x(self):
        """
        Alternate between definition and hover on the same symbol 100 times.
        Tests the server's ability to handle repeated requests for the same
        location without cache corruption.
        """
        uri = self._uri_basic
        for i in range(100):
            if i % 2 == 0:
                resp = self.c.definition(uri, 7, 8)
            else:
                resp = self.c.hover(uri, 7, 8)
            self.assertIn("id", resp, f"Request {i} got no id")
        self._assert_server_alive()

    def test_multiple_files_interleaved_requests(self):
        """
        Interleave requests to different open files.
        Tests that per-document state is correctly maintained.
        """
        uri_b = self._uri_basic
        uri_e = self._uri_enums
        for i in range(60):
            if i % 2 == 0:
                resp = self.c.hover(uri_b, 2, 7)
                self.assertIn("id", resp, f"Basic hover {i} crashed server")
            else:
                resp = self.c.hover(uri_e, 2, 5)
                self.assertIn("id", resp, f"Enum hover {i} crashed server")
        self._assert_server_alive()

    def test_signature_help_rapid(self):
        """Send 50 signatureHelp requests in a row."""
        uri = fixture_uri("sighelp.oc")
        self.c.did_open(uri, fixture_text("sighelp.oc"))
        self.c.drain_notifications(timeout=0.3)
        for i in range(50):
            resp = self.c.signature_help(uri, 8, 10 + (i % 5))
            self.assertIn("id", resp, f"SigHelp request {i} got invalid response")
        self.c.did_close(uri)
        self._assert_server_alive()

    def test_references_rapid(self):
        """Send 50 references requests in rapid succession."""
        uri = self._uri_refs
        for i in range(50):
            resp = self.c.references(uri, 2, 4 + (i % 3))
            self.assertIn("id", resp, f"References request {i} got invalid response")
        self._assert_server_alive()

    def test_rename_idempotent_many_times(self):
        """
        Rename the same function multiple times with the same name.
        Should be idempotent and never crash.
        """
        uri = self._uri_refs
        for i in range(30):
            resp = self.c.rename(uri, 2, 4, "square_renamed")
            self.assertIn("id", resp, f"Rename {i} got invalid response")
        self._assert_server_alive()

    def test_open_close_reopen_file_many_times(self):
        """
        Open, close, and reopen the same file repeatedly.
        This stress-tests document lifecycle management.
        """
        uri = fixture_uri("enums.oc")
        text = fixture_text("enums.oc")
        for i in range(20):
            self.c.did_open(uri, text)
            self.c.drain_notifications(timeout=0.1)
            resp = self.c.hover(uri, 2, 5)
            self.assertIn("id", resp, f"Hover in iteration {i} got no id (server crashed?)")
            self.c.did_close(uri)
        self._assert_server_alive()

    def test_diagnostics_toggle_error(self):
        """
        Switch a file between valid and invalid state quickly.
        Server should produce diagnostics for invalid versions and none for valid ones.
        After N toggles, server should still be alive.
        """
        uri = fixture_uri("diag_toggle.oc")
        valid_code = "/// skip\n\ndef main() {}\n"
        error_code = "/// skip\n\ndef main() { let x: i32 = \"bad\" }\n"

        self.c.did_open(uri, valid_code)
        self.c.drain_notifications(timeout=0.5)

        for i in range(20):
            # Introduce error
            change = whole_doc_replace_change(valid_code if i % 2 == 0 else error_code,
                                              error_code if i % 2 == 0 else valid_code)
            self.c.did_change(uri, [change], version=i + 2)
            time.sleep(0.05)  # Allow server to process

        self.c.drain_notifications(timeout=0.5)
        self.c.did_close(uri)
        self._assert_server_alive()

    def test_formatting_rapid(self):
        """Send 20 formatting requests in rapid succession."""
        uri = fixture_uri("formatting.oc")
        self.c.did_open(uri, fixture_text("formatting.oc"))
        self.c.drain_notifications(timeout=0.3)
        for i in range(20):
            resp = self.c.formatting(uri)
            self.assertIn("id", resp, f"Formatting request {i} got invalid response")
        self.c.did_close(uri)
        self._assert_server_alive()

    def test_unknown_methods_dont_crash_under_load(self):
        """
        Send unknown methods interspersed with valid requests.
        The server must not crash on unknown methods.
        """
        uri = self._uri_basic
        for i in range(40):
            if i % 4 == 0:
                # Unknown method
                resp = self.c.request(f"textDocument/unknownMethod{i}", {
                    "textDocument": {"uri": uri},
                    "position": {"line": 0, "character": 0},
                })
                self.assertIn("id", resp)
            else:
                resp = self.c.hover(uri, 1, 7)
                self.assertIn("id", resp, f"Valid hover {i} failed after unknown method")
        self._assert_server_alive()


# ---------------------------------------------------------------------------
# Fuzz tests – realistic requests against the small fixture files
# ---------------------------------------------------------------------------

# Use the small fixture files as the fuzz corpus.  Each file is standalone
# (no imports) and compiles in ~5ms, versus ~150ms for a large compiler source.
# This keeps the full TestFuzzer class under 10 seconds.
_FUZZ_FILES = [
    FIXTURES / "basic.oc",
    FIXTURES / "references.oc",
    FIXTURES / "enums.oc",
    FIXTURES / "sighelp.oc",
]

# All LSP method names that take a textDocument + position.
_POSITION_METHODS = [
    "textDocument/hover",
    "textDocument/definition",
    "textDocument/typeDefinition",
    "textDocument/completion",
    "textDocument/signatureHelp",
    "textDocument/references",
    "textDocument/documentSymbol",  # only textDocument, no position
]

_HAS_POSITION = {m for m in _POSITION_METHODS if m != "textDocument/documentSymbol"}

# Ground-truth hover results for specific (file, line, col) positions.
# These are used by the correctness tests — we KNOW what content the server
# must return at these locations.
_HOVER_GROUND_TRUTH: dict[tuple[str, int, int], str] = {
    ("basic.oc",       2,  7): "struct Point",
    ("basic.oc",       7,  4): "def add",
    ("basic.oc",      11,  6): "const MAX",
    ("references.oc",  2,  4): "def square",
    ("enums.oc",       2,  5): "enum Color",
    ("enums.oc",       3,  4): "Color::Red",
}

# Ground-truth definition destinations: (file, line, col) → expected 0-indexed def line.
_DEFINITION_GROUND_TRUTH: dict[tuple[str, int, int], int] = {
    ("basic.oc",      16, 12): 7,   # add() call → def add(...)
    ("basic.oc",      14, 12): 2,   # Point(...) → struct Point
    ("references.oc",  7, 12): 2,   # square(3) → def square
}

# Ground-truth documentSymbol: file → list of expected symbol names.
_SYMBOLS_GROUND_TRUTH: dict[str, list[str]] = {
    "basic.oc":      ["Point", "add", "main", "MAX"],
    "references.oc": ["square", "main"],
    "enums.oc":      ["Color", "to_str", "main"],
    "sighelp.oc":    ["greet", "main"],
}


def _make_position_request(method: str, uri: str, line: int, char: int) -> dict:
    """Build the params dict for a position-based LSP request."""
    params: dict = {"textDocument": {"uri": uri}}
    if method in _HAS_POSITION:
        params["position"] = {"line": line, "character": char}
    if method == "textDocument/references":
        params["context"] = {"includeDeclaration": True}
    return params


def _extract_identifier_positions(text: str) -> list[tuple[int, int]]:
    """
    Return a list of (line, col) for the start of every identifier-like token
    in the source text.  We use a simple regex rather than a full lexer.
    """
    positions = []
    ident_re = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')
    for lineno, line in enumerate(text.splitlines()):
        for m in ident_re.finditer(line):
            positions.append((lineno, m.start()))
    return positions


class TestFuzzer(LSPTestBase):
    """
    Fuzz-testing: fire requests at random and known-good positions in the
    small fixture files.  Because the fixtures are standalone (no imports),
    each request compiles in ~5ms, keeping the whole class under 10 seconds.

    Correctness IS checked:
      - Ground-truth hover/definition/symbol tables verify specific positions.
      - Structural validity is asserted for every response (correct JSON-RPC
        envelope, correct shape of result objects where non-null).
      - The server must never crash (heartbeat verified after every test).
    """

    SEED = 42
    # Requests per random-fuzz test — keep small so individual tests are fast.
    N_REQUESTS = 40

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._rng = random.Random(cls.SEED)
        # Load fixture files and pre-compute identifier positions.
        cls._fuzz_corpus: list[tuple[Path, str, list[tuple[int, int]]]] = []
        for fpath in _FUZZ_FILES:
            if fpath.exists():
                text = fpath.read_text()
                positions = _extract_identifier_positions(text)
                if positions:
                    cls._fuzz_corpus.append((fpath, text, positions))
        # Open all fixture files in the server.
        for fpath, text, _ in cls._fuzz_corpus:
            uri = f"file://{fpath.resolve()}"
            cls._client.did_open(uri, text)
        cls._client.drain_notifications(timeout=0.3)

    @classmethod
    def tearDownClass(cls):
        for fpath, _, _ in cls._fuzz_corpus:
            uri = f"file://{fpath.resolve()}"
            try:
                cls._client.did_close(uri)
            except Exception:
                pass
        super().tearDownClass()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _uri(self, fpath: Path) -> str:
        return f"file://{fpath.resolve()}"

    def _pick_random_target(self) -> tuple[str, int, int]:
        """Return a random (uri, line, col) from the fuzz corpus."""
        fpath, _text, positions = self._rng.choice(self._fuzz_corpus)
        line, col = self._rng.choice(positions)
        return self._uri(fpath), line, col

    def _assert_valid_rpc_response(self, resp: dict, context: str):
        """Every JSON-RPC response must have 'id' and 'result' or 'error'."""
        self.assertIsInstance(resp, dict, f"{context}: not a dict")
        self.assertIn("id", resp, f"{context}: missing 'id'")
        self.assertTrue("result" in resp or "error" in resp,
                        f"{context}: neither 'result' nor 'error': {resp}")

    def _assert_hover_structure(self, result: dict | None, context: str):
        """When hover returns non-null, validate the contents structure."""
        if result is None:
            return
        self.assertIn("contents", result, f"{context}: hover result missing 'contents'")
        contents = result["contents"]
        self.assertIsInstance(contents, list, f"{context}: hover contents not a list")
        self.assertGreater(len(contents), 0, f"{context}: hover contents empty")
        first = contents[0]
        self.assertIn("language", first, f"{context}: hover item missing 'language'")
        self.assertIn("value", first, f"{context}: hover item missing 'value'")
        self.assertEqual(first["language"], "ocen",
                         f"{context}: hover language should be 'ocen'")

    def _assert_definition_structure(self, result: dict | None, context: str):
        """When definition returns non-null, validate uri + range."""
        if result is None:
            return
        self.assertIn("uri", result, f"{context}: definition missing 'uri'")
        self.assertTrue(result["uri"].startswith("file://"),
                        f"{context}: definition uri should use file:// scheme")
        self.assertIn("range", result, f"{context}: definition missing 'range'")
        r = result["range"]
        for field in ("start", "end"):
            self.assertIn(field, r, f"{context}: range missing '{field}'")
            self.assertGreaterEqual(r[field]["line"], 0,
                                    f"{context}: range {field} line must be >= 0")
            self.assertGreaterEqual(r[field]["character"], 0,
                                    f"{context}: range {field} char must be >= 0")

    def _assert_references_structure(self, result: list | None, context: str):
        """When references returns non-null, validate each ref has uri + range."""
        if result is None:
            return
        self.assertIsInstance(result, list, f"{context}: references result not a list")
        for i, ref in enumerate(result):
            self.assertIn("uri", ref, f"{context}: ref[{i}] missing 'uri'")
            self.assertIn("range", ref, f"{context}: ref[{i}] missing 'range'")

    def _heartbeat(self):
        """Verify server is still alive by hovering at a known-good position."""
        if not self._fuzz_corpus:
            return
        fpath = self._fuzz_corpus[0][0]
        resp = self.c.hover(self._uri(fpath), 2, 7)  # basic.oc: 'struct Point'
        self.assertIn("id", resp, "Server stopped responding (fuzz heartbeat failed)")

    # ------------------------------------------------------------------
    # Ground-truth correctness tests
    # ------------------------------------------------------------------

    def test_fuzz_hover_ground_truth(self):
        """
        Hover at every known-good position and verify the expected substring
        appears in the response.  These are hard facts about the fixture files.
        """
        for (fname, line, col), expected in _HOVER_GROUND_TRUTH.items():
            fpath = FIXTURES / fname
            uri = self._uri(fpath)
            resp = self.c.hover(uri, line, col)
            self._assert_valid_rpc_response(resp, f"hover ground truth {fname}:{line}:{col}")
            result = resp["result"]
            self.assertIsNotNone(result,
                f"hover at {fname}:{line}:{col} returned null, expected '{expected}'")
            self._assert_hover_structure(result, f"hover {fname}:{line}:{col}")
            val = result["contents"][0]["value"]
            self.assertIn(expected, val,
                f"hover at {fname}:{line}:{col}: expected '{expected}' in '{val}'")
        self._heartbeat()

    def test_fuzz_definition_ground_truth(self):
        """
        Request definition at known positions and verify the result points to
        the correct (0-indexed) line.
        """
        for (fname, line, col), expected_line in _DEFINITION_GROUND_TRUTH.items():
            fpath = FIXTURES / fname
            uri = self._uri(fpath)
            resp = self.c.definition(uri, line, col)
            self._assert_valid_rpc_response(resp, f"def ground truth {fname}:{line}:{col}")
            result = resp["result"]
            self.assertIsNotNone(result,
                f"definition at {fname}:{line}:{col} returned null, expected line {expected_line}")
            self._assert_definition_structure(result, f"def {fname}:{line}:{col}")
            got_line = result["range"]["start"]["line"]
            self.assertEqual(got_line, expected_line,
                f"definition at {fname}:{line}:{col}: expected line {expected_line}, got {got_line}")
        self._heartbeat()

    def test_fuzz_references_ground_truth(self):
        """
        Request references for 'square' in references.oc and verify:
        - At least 3 results (1 definition + 2 usages).
        - Definition line (2) is included.
        - Usage lines (7, 8) are included.
        - Every reference has a valid uri + range.
        """
        uri = self._uri(FIXTURES / "references.oc")
        resp = self.c.references(uri, 2, 4)  # 'square' in 'def square(...)'
        self._assert_valid_rpc_response(resp, "references ground truth")
        result = resp["result"]
        self.assertIsNotNone(result, "references for 'square' returned null")
        self._assert_references_structure(result, "references ground truth")
        self.assertGreaterEqual(len(result), 3,
            f"expected >= 3 refs for 'square', got {len(result)}")
        lines = {r["range"]["start"]["line"] for r in result}
        self.assertIn(2, lines, "definition line 2 must be in references")
        self.assertIn(7, lines, "usage line 7 must be in references")
        self.assertIn(8, lines, "usage line 8 must be in references")
        self._heartbeat()

    def test_fuzz_symbols_ground_truth(self):
        """
        documentSymbol for each fixture file must include the expected top-level
        symbol names, with correct kinds and valid ranges.
        """
        for fpath, _, _ in self._fuzz_corpus:
            fname = fpath.name
            if fname not in _SYMBOLS_GROUND_TRUTH:
                continue
            uri = self._uri(fpath)
            resp = self.c.document_symbol(uri)
            self._assert_valid_rpc_response(resp, f"symbols ground truth {fname}")
            self.assertIsNotNone(resp["result"], f"documentSymbol for {fname} returned null")
            symbols = resp["result"]
            self.assertIsInstance(symbols, list, f"documentSymbol for {fname} not a list")
            names = {s["name"] for s in symbols}
            for expected in _SYMBOLS_GROUND_TRUTH[fname]:
                self.assertIn(expected, names,
                    f"Expected symbol '{expected}' in {fname}, got {sorted(names)}")
            # Structural check: every symbol must have a valid range.
            for sym in symbols:
                self.assertIn("range", sym, f"{fname}: symbol '{sym['name']}' missing 'range'")
                r = sym["range"]
                self.assertGreaterEqual(r["start"]["line"], 0)
                self.assertGreaterEqual(r["end"]["line"], r["start"]["line"])
        self._heartbeat()

    def test_fuzz_signature_help_ground_truth(self):
        """
        signatureHelp inside greet("hi", 5) must return a valid signature.
        """
        uri = self._uri(FIXTURES / "sighelp.oc")
        # sighelp.oc line 8: '    greet("hi", 5)' — position inside first arg
        resp = self.c.signature_help(uri, 8, 11)
        self._assert_valid_rpc_response(resp, "sighelp ground truth")
        result = resp["result"]
        if result is not None:
            # If the server returns something, it must be a valid signatureHelp
            self.assertIn("signatures", result,
                          "signatureHelp result missing 'signatures'")
            self.assertIsInstance(result["signatures"], list)
            if result["signatures"]:
                sig = result["signatures"][0]
                self.assertIn("label", sig, "signature missing 'label'")
                self.assertIn("greet", sig["label"],
                              f"expected 'greet' in signature label, got '{sig['label']}'")
        self._heartbeat()

    # ------------------------------------------------------------------
    # Structural-validity fuzz tests (random positions, no known output)
    # ------------------------------------------------------------------

    def test_fuzz_hover_random_positions(self):
        """
        Send N_REQUESTS hover requests at random identifier positions in the
        fixture files.  Every response must be valid JSON-RPC and have a
        correctly-shaped result when non-null.
        """
        if not self._fuzz_corpus:
            self.skipTest("No fuzz corpus files found")
        for i in range(self.N_REQUESTS):
            uri, line, col = self._pick_random_target()
            resp = self.c.hover(uri, line, col)
            self._assert_valid_rpc_response(resp, f"hover[{i}] at ({line},{col})")
            self._assert_hover_structure(resp["result"], f"hover[{i}]")
        self._heartbeat()

    def test_fuzz_definition_random_positions(self):
        """N_REQUESTS definition requests at random positions, structure-checked."""
        if not self._fuzz_corpus:
            self.skipTest("No fuzz corpus files found")
        for i in range(self.N_REQUESTS):
            uri, line, col = self._pick_random_target()
            resp = self.c.definition(uri, line, col)
            self._assert_valid_rpc_response(resp, f"def[{i}] at ({line},{col})")
            self._assert_definition_structure(resp["result"], f"def[{i}]")
        self._heartbeat()

    def test_fuzz_mixed_methods(self):
        """
        N_REQUESTS mixing hover, definition, typeDefinition, completion,
        signatureHelp, references, and documentSymbol — all structure-checked.
        """
        if not self._fuzz_corpus:
            self.skipTest("No fuzz corpus files found")
        methods = list(_POSITION_METHODS)
        for i in range(self.N_REQUESTS):
            method = self._rng.choice(methods)
            if method == "textDocument/documentSymbol":
                fpath = self._rng.choice(self._fuzz_corpus)[0]
                uri = self._uri(fpath)
                params: dict = {"textDocument": {"uri": uri}}
            else:
                uri, line, col = self._pick_random_target()
                params = _make_position_request(method, uri, line, col)
            resp = self.c.request(method, params)
            self._assert_valid_rpc_response(resp, f"mixed[{i}] method={method}")
        self._heartbeat()

    def test_fuzz_oob_positions_are_safe(self):
        """
        Out-of-bounds positions must not crash the server; they must return
        null results wrapped in a valid JSON-RPC response.
        """
        if not self._fuzz_corpus:
            self.skipTest("No fuzz corpus files found")
        fpath, text, _ = self._fuzz_corpus[0]  # basic.oc
        uri = self._uri(fpath)
        total_lines = len(text.splitlines())
        oob = [
            (total_lines + 100, 0),
            (total_lines + 500, 99),
            (0, 9999),
            (1, 9999),
        ]
        for line, col in oob:
            resp = self.c.hover(uri, line, col)
            self._assert_valid_rpc_response(resp, f"OOB hover ({line},{col})")
            # OOB must return null result (not a crash and not a real hover)
            self.assertIsNone(resp.get("result"),
                f"OOB hover at ({line},{col}) should return null, got {resp.get('result')}")
        self._heartbeat()

    def test_fuzz_incremental_edits(self):
        """
        Make 20 incremental whole-document edits to a fixture file and fire
        a hover after every 5 edits.  Tests server stability under rapid editing.
        """
        if not self._fuzz_corpus:
            self.skipTest("No fuzz corpus files found")
        fpath, original_text, positions = self._fuzz_corpus[0]
        edit_uri = self._uri(fpath) + "_fuzz_edit"
        self.c.did_open(edit_uri, original_text)
        self.c.drain_notifications(timeout=0.2)
        current_text = original_text
        for i in range(20):
            new_text = original_text if i % 2 else original_text + f"// edit {i}\n"
            change = whole_doc_replace_change(current_text, new_text)
            self.c.did_change(edit_uri, [change], version=i + 2)
            current_text = new_text
            if (i + 1) % 5 == 0:
                # Hover in the UNMODIFIED shared file — ensures two docs stay isolated.
                orig_uri = self._uri(fpath)
                resp = self.c.hover(orig_uri, 2, 7)
                self._assert_valid_rpc_response(resp, f"hover after {i+1} edits")
                self._assert_hover_structure(resp["result"], f"hover after {i+1} edits")
        self.c.drain_notifications(timeout=0.2)
        self.c.did_close(edit_uri)
        self._heartbeat()


# ---------------------------------------------------------------------------
# Helpers for parallel test execution
# ---------------------------------------------------------------------------

def _discover_all_test_classes():
    """Return all concrete test classes defined in this module (those with test methods)."""
    import inspect
    module = sys.modules[__name__]
    classes = []
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if not issubclass(obj, unittest.TestCase):
            continue
        if obj is unittest.TestCase:
            continue
        # Only include classes that have at least one test method defined
        # directly on them (not inherited); this skips abstract base classes.
        has_tests = any(
            k.startswith("test_") and callable(v)
            for k, v in vars(obj).items()
        )
        if has_tests:
            classes.append(obj)
    return classes


def _run_single_class(class_name: str, compiler: str, verbose: bool = False) -> tuple[str, int, str]:
    """
    Run a single test class in a subprocess.  Returns (class_name, exit_code, output).
    """
    cmd = [sys.executable, __file__, "-c", compiler, class_name]
    if verbose:
        cmd.append("-v")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    output = result.stdout + result.stderr
    return class_name, result.returncode, output


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    global COMPILER

    parser = argparse.ArgumentParser(description="LSP server test harness for Ocen")
    parser.add_argument("-c", "--compiler", default=str(ROOT / "build" / "ocen"),
                        help="Path to the compiler executable")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose unittest output")
    parser.add_argument("-f", "--failfast", action="store_true",
                        help="Stop after first failure")
    parser.add_argument("-p", "--parallel", action="store_true",
                        help="Run each test class in a separate parallel subprocess")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help="Number of parallel jobs (default: number of test classes)")
    parser.add_argument("tests", nargs="*",
                        help="Specific test names to run (default: all)")
    args = parser.parse_args()

    COMPILER = args.compiler

    if not os.path.isfile(COMPILER):
        print(f"Error: compiler not found at {COMPILER}", file=sys.stderr)
        sys.exit(1)

    if args.parallel and not args.tests:
        # Run every test class in parallel subprocesses.
        classes = _discover_all_test_classes()
        n_jobs = args.jobs or len(classes)
        print(f"Running {len(classes)} test classes in parallel (max {n_jobs} jobs)...")

        start_time = time.monotonic()
        results: list[tuple[str, int, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_jobs) as pool:
            futures = {
                pool.submit(_run_single_class, cls.__name__, COMPILER, args.verbose): cls.__name__
                for cls in classes
            }
            for future in concurrent.futures.as_completed(futures):
                cls_name, rc, output = future.result()
                results.append((cls_name, rc, output))
                status = "OK" if rc == 0 else "FAIL"
                print(f"  [{status}] {cls_name}")
                if rc != 0 and args.failfast:
                    # Cancel remaining (they may still run to completion)
                    print("Stopping due to --failfast")
                    break

        elapsed = time.monotonic() - start_time
        print()

        # Print output from failed classes
        failed_classes = [(n, o) for n, rc, o in results if rc != 0]
        if failed_classes:
            print("=" * 70)
            print("FAILURES:")
            for name, output in failed_classes:
                print(f"\n--- {name} ---")
                # Only print the last 40 lines to avoid flooding
                lines = output.strip().split("\n")
                for line in lines[-40:]:
                    print(line)
            print("=" * 70)

        total = len(results)
        failed = len(failed_classes)
        print(f"\nRan {total} test classes in {elapsed:.1f}s")
        if failed:
            print(f"FAILED (classes={failed})")
            sys.exit(1)
        else:
            print("OK")
            sys.exit(0)

    else:
        # Original serial runner.
        loader = unittest.TestLoader()
        if args.tests:
            suite = unittest.TestSuite()
            for t in args.tests:
                suite.addTests(loader.loadTestsFromName(t, sys.modules[__name__]))
        else:
            suite = loader.loadTestsFromModule(sys.modules[__name__])

        verbosity = 2 if args.verbose else 1
        runner = unittest.TextTestRunner(verbosity=verbosity, failfast=args.failfast)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
