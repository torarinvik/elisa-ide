#!/usr/bin/env python3
"""Contract tests for the IDE's bounded LSP transport and document bridge."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from dataclasses import replace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from lsp import DiagnosticStore, FrameDecoder, FrameError, LspClient, LspError, path_to_uri  # noqa: E402
from lsp.lsp_client import frame  # noqa: E402
from source import SourceDocument  # noqa: E402
from lsp import PublishedDiagnostic  # noqa: E402


FAKE_SERVER = textwrap.dedent(
    r'''
    import json, sys

    def read_message():
        header = sys.stdin.buffer.readline()
        if not header:
            return None
        if header != b"Content-Length: " + header.split(b":", 1)[1]:
            pass
        if not header.endswith(b"\r\n"):
            return None
        length = int(header.split(b":", 1)[1].strip())
        if sys.stdin.buffer.readline() != b"\r\n":
            return None
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))

    def send(message, fragmented=False):
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        raw = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        if fragmented:
            for byte in raw:
                sys.stdout.buffer.write(bytes([byte]))
                sys.stdout.buffer.flush()
        else:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc":"2.0", "id":message["id"], "result":{
                "serverInfo":{"name":"fake-elisa-lsp","version":"test"},
                "capabilities":{"positionEncoding":"utf-8","textDocumentSync":2,"hoverProvider":True,
                    "definitionProvider":True,
                    "workspace":{"workspaceFolders":{"supported":True,"changeNotifications":True}},
                    "semanticTokensProvider":{"legend":{"tokenTypes":["type"],"tokenModifiers":["readonly"]}}}
            }}, fragmented=True)
        elif method == "initialized":
            send({"jsonrpc":"2.0", "id":"register-config", "method":"client/registerCapability",
                "params":{"registrations":[{"id":"config-1","method":"workspace/didChangeConfiguration"}]}})
        elif method == "workspace/didChangeConfiguration":
            send({"jsonrpc":"2.0", "method":"fake/configurationSeen", "params":message["params"]})
            send({"jsonrpc":"2.0", "id":"settings-query", "method":"workspace/configuration",
                "params":{"items":[{"section":"elisa.build"},{"section":"elisa.missing"}]}})
        elif method == "workspace/didChangeWorkspaceFolders":
            send({"jsonrpc":"2.0", "method":"fake/workspaceFoldersSeen", "params":message["params"]})
        elif "id" in message and message["id"] == "settings-query":
            send({"jsonrpc":"2.0", "method":"fake/settingsResult", "params":{"result":message.get("result")}})
        elif method == "textDocument/didOpen":
            document = message["params"]["textDocument"]
            send({"jsonrpc":"2.0", "method":"textDocument/publishDiagnostics", "params":{
                "uri":document["uri"], "version":1,
                "diagnostics":[{"message":"first","severity":2,"source":"elisa-lsp","code":"E1",
                    "range":{"start":{"line":1,"character":2},"end":{"line":1,"character":8}},
                    "relatedInformation":[{"message":"related"}]}]
            }}, fragmented=True)
        elif method == "textDocument/didChange":
            document = message["params"]["textDocument"]
            send({"jsonrpc":"2.0", "method":"fake/didChangeSeen", "params":message["params"]})
            # A stale result and the current result share the same stream. The
            # client must retain only the matching document version.
            send({"jsonrpc":"2.0", "method":"textDocument/publishDiagnostics", "params":{
                "uri":document["uri"], "version":1,
                "diagnostics":[{"message":"stale"}]
            }})
            send({"jsonrpc":"2.0", "method":"textDocument/publishDiagnostics", "params":{
                "uri":document["uri"], "version":document["version"],
                "diagnostics":[{"message":"current","severity":1,"source":"elisa-lsp","code":7,
                    "range":{"start":{"line":0,"character":1},"end":{"line":0,"character":3}}}]
            }})
        elif method == "textDocument/hover":
            send({"jsonrpc":"2.0", "id":message["id"], "result":{"contents":"hovered"}}, fragmented=True)
        elif method == "shutdown":
            send({"jsonrpc":"2.0", "id":message["id"], "result":None})
        elif method == "exit":
            break
    ''',
)


FAKE_WORKSPACE_SYMBOL_SERVER = textwrap.dedent(
    r'''
    import json, sys

    def read_message():
        header = sys.stdin.buffer.readline()
        if not header or not header.startswith(b"Content-Length: ") or not header.endswith(b"\r\n"):
            return None
        length = int(header.split(b":", 1)[1].strip())
        if sys.stdin.buffer.readline() != b"\r\n":
            return None
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))

    def send(message):
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sys.stdout.buffer.flush()

    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc":"2.0", "id":message["id"], "result":{
                "capabilities":{"workspaceSymbolProvider":True}
            }})
        elif method == "workspace/symbol":
            query = message.get("params", {}).get("query")
            send({"jsonrpc":"2.0", "id":message["id"], "result":[{
                "name":"main", "kind":12, "containerName":"sample",
                "location":{"uri":"file:///workspace/main.elisa",
                    "range":{"start":{"line":0,"character":4},"end":{"line":0,"character":8}}}
            }] if query == "main" else []})
        elif method == "shutdown":
            send({"jsonrpc":"2.0", "id":message["id"], "result":None})
        elif method == "exit":
            break
    ''',
)


def test_decoder() -> None:
    message = {"jsonrpc": "2.0", "id": 1, "result": {"text": "Grüße 😀"}}
    decoder = FrameDecoder()
    decoded = []
    encoded = frame(message)
    for byte in encoded:
        decoded.extend(decoder.feed(bytes([byte])))
    decoder.finish()
    assert len(decoded) == 1 and decoded[0].message == message
    try:
        decoder.feed(b"Content-Length: 1\r\nContent-Length: 1\r\n\r\n{}")
    except FrameError:
        pass
    else:
        raise AssertionError("duplicate Content-Length was accepted")


def test_diagnostic_byte_ranges() -> None:
    uri = "file:///workspace/unicode.elisa"
    document = SourceDocument("a😀e\u0301\n")
    raw = {
        "message": "problem in emoji",
        "severity": 1,
        "source": "elisa-lsp",
        "code": "E-UNICODE",
        "range": {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": 2}},
        "relatedInformation": [{"message": "related context"}],
    }
    diagnostic = PublishedDiagnostic.from_mapping(uri, 7, raw)
    assert diagnostic is not None
    emoji_end = len("a😀".encode("utf-8"))
    assert diagnostic.byte_range(document, encoding="utf-32") == (1, emoji_end)
    utf16 = PublishedDiagnostic.from_mapping(uri, 7, {
        **raw, "range": {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": 3}},
    })
    utf8 = PublishedDiagnostic.from_mapping(uri, 7, {
        **raw, "range": {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": 5}},
    })
    assert utf16 is not None and utf16.byte_range(document, encoding="utf-16") == (1, emoji_end)
    assert utf8 is not None and utf8.byte_range(document, encoding="utf-8") == (1, emoji_end)
    assert diagnostic.version == 7 and diagnostic.related_information[0]["message"] == "related context"

    split_surrogate = PublishedDiagnostic.from_mapping(uri, 7, {
        "message": "invalid boundary",
        "range": {
            "start": {"line": 0, "character": 2},
            "end": {"line": 0, "character": 3},
        },
    })
    assert split_surrogate is not None
    assert split_surrogate.byte_range(document, encoding="utf-16") is None

    reversed_range = PublishedDiagnostic.from_mapping(uri, 7, {
        "message": "reversed",
        "range": {
            "start": {"line": 0, "character": 2},
            "end": {"line": 0, "character": 1},
        },
    })
    assert reversed_range is not None
    assert reversed_range.byte_range(document, encoding="utf-32") is None


def test_diagnostic_version_types() -> None:
    store = DiagnosticStore()
    uri = "file:///workspace/version.elisa"
    store.set_document_version(uri, 1)
    assert not store.publish(uri, True, [{"message": "bool is not an LSP integer version"}])
    assert not store.publish(uri, None, [{"message": "missing version"}])
    assert store.publish(uri, 1, [{"message": "current"}])
    assert store.typed(uri)[0].message == "current"
    store.set_document_version(uri, 2)
    assert not store.typed(uri) and not store.diagnostics(uri)
    try:
        store.set_document_version(uri, True)
    except ValueError:
        pass
    else:
        raise AssertionError("boolean document version was accepted")


def test_client() -> None:
    uri = path_to_uri(os.path.join(ROOT, "test", "fixtures", "settings-form.elisaform.json"))
    with LspClient([sys.executable, "-u", "-c", FAKE_SERVER], startup_timeout=2.0) as client:
        initialized = client.initialize({"capabilities": {}})
        assert initialized["result"]["serverInfo"]["name"] == "fake-elisa-lsp"
        assert client.capabilities["hoverProvider"] is True
        assert client.server_capabilities.position_encoding == "utf-8"
        assert client.server_capabilities.document_sync_kind == 2
        assert client.server_capabilities.semantic_token_types == ("type",)
        assert client.server_capabilities.workspace_folders_supported is True
        assert client.server_capabilities.workspace_folder_change_notifications is True
        try:
            client.update_configuration({"elisa": {"build": {"target": "host"}}})
        except LspError as exc:
            assert "has not registered" in str(exc)
        else:
            raise AssertionError("configuration was sent before server registration")
        body_limit = client.decoder.max_body_bytes
        client.decoder.max_body_bytes = 32
        try:
            client.notify("test/oversized", {"value": "x" * 64})
        except FrameError as exc:
            assert "outgoing LSP body" in str(exc)
        else:
            raise AssertionError("oversized outgoing LSP notification was sent")
        finally:
            client.decoder.max_body_bytes = body_limit
        # The fake server handles one capability-gated request; unsupported
        # requests must be refused locally before they reach its stdin.
        hover = client.hover(uri, 0, 0)
        assert hover["result"]["contents"] == "hovered"
        assert client.update_workspace_folders(added=[{"uri": "file:///workspace/lib", "name": "lib"}]) == (
            "file:///workspace/lib",
        )
        assert client.update_workspace_folders(removed=["file:///workspace/lib"]) == ()
        try:
            client.workspace_symbols("main")
        except Exception as exc:
            assert "workspaceSymbolProvider" in str(exc)
        else:
            raise AssertionError("unsupported workspace symbols were requested")
        old_text = "a😀e\u0301\r\nnext😀"
        for encoding, end_character in (("utf-8", 8), ("utf-16", 6), ("utf-32", 5)):
            client.server_capabilities = replace(
                client.server_capabilities, position_encoding=encoding
            )
            assert client._end_position(old_text) == {
                "line": 1, "character": end_character
            }
        client.server_capabilities = replace(
            client.server_capabilities, position_encoding="utf-8"
        )
        client.open_document(uri, old_text)
        note = client.wait_notification("textDocument/publishDiagnostics")
        assert note["params"]["version"] == 1
        assert client.diagnostics.diagnostics(uri)[0]["message"] == "first"
        first = client.diagnostics.typed(uri)[0]
        assert first.version == 1 and first.severity == 2 and first.source == "elisa-lsp"
        assert first.code == "E1" and first.start_line == 1 and first.start_character == 2
        assert first.end_line == 1 and first.end_character == 8
        assert first.related_information == ({"message": "related"},)
        assert client.change_document(uri, "def main() -> i64:\n    return 1\n") == 2
        change = client.wait_notification("fake/didChangeSeen")["params"]["contentChanges"][0]
        assert change["range"] == {
            "start": {"line": 0, "character": 0},
            "end": {"line": 1, "character": 8},
        }
        client.wait_notification("textDocument/publishDiagnostics")
        client.wait_notification("textDocument/publishDiagnostics")
        assert client.diagnostics.diagnostics(uri)[0]["message"] == "current"
        current = client.diagnostics.typed(uri)[0]
        assert current.version == 2 and current.severity == 1 and current.code == 7
        assert current.start_line == 0 and current.start_character == 1
        client.save_document(uri)
        client.update_configuration({"elisa": {"build": {"target": "host"}}})
        assert client.wait_notification("fake/configurationSeen")["params"]["settings"]["elisa"]["build"]["target"] == "host"
        assert client.wait_notification("fake/workspaceFoldersSeen")["params"]["event"]["added"] == [
            {"uri": "file:///workspace/lib", "name": "lib"}
        ]
        assert client.wait_notification("fake/workspaceFoldersSeen")["params"]["event"]["removed"] == [
            {"uri": "file:///workspace/lib", "name": "lib"}
        ]
        assert client.wait_notification("fake/settingsResult")["params"]["result"] == [
            {"target": "host"}, None
        ]
        client.close_document(uri)
        assert uri not in client.documents
        assert client.shutdown()["result"] is None
        assert client.close() == 0


def test_workspace_symbols() -> None:
    with LspClient(
        [sys.executable, "-u", "-c", FAKE_WORKSPACE_SYMBOL_SERVER],
        startup_timeout=2.0,
    ) as client:
        client.initialize({"capabilities": {}})
        assert client.server_capabilities.supports("workspaceSymbolProvider")
        response = client.workspace_symbols("main")
        assert response["result"] == [{
            "name": "main",
            "kind": 12,
            "containerName": "sample",
            "location": {
                "uri": "file:///workspace/main.elisa",
                "range": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 8},
                },
            },
        }]
        assert client.shutdown()["result"] is None
        assert client.close() == 0


def main() -> int:
    test_decoder()
    test_diagnostic_byte_ranges()
    test_diagnostic_version_types()
    test_client()
    test_workspace_symbols()
    print("lsp client: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
