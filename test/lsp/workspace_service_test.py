#!/usr/bin/env python3
"""Source-buffer, LSP synchronization, and diagnostic projection contract."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from lsp import LspSupervisor, path_to_uri  # noqa: E402
from lsp.workspace_service import LspWorkspace  # noqa: E402
from problems import ProblemModel, ProblemSeverity  # noqa: E402
from source import SourceEdit  # noqa: E402


SERVER = textwrap.dedent(
    r'''
    import json, os, sys

    def read_message():
        length = None
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line == b"\r\n":
                break
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                length = int(value.strip())
        if length is None:
            return None
        return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

    def send(message):
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sys.stdout.buffer.flush()

    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc":"2.0", "id":message["id"], "result":{
                "capabilities":{"positionEncoding":"utf-16", "textDocumentSync":1,
                    "workspaceSymbolProvider":True}
            }})
        elif method in ("textDocument/didOpen", "textDocument/didChange"):
            document = message["params"]["textDocument"]
            with open(os.environ["LSP_WORKSPACE_OPEN_LOG"], "a", encoding="utf-8") as log:
                log.write(json.dumps(document, ensure_ascii=False) + "\n")
                log.flush()
            version = document["version"]
            send({"jsonrpc":"2.0", "method":"textDocument/publishDiagnostics", "params":{
                "uri":document["uri"], "version":version,
                "diagnostics":[{"message":"version " + str(version),
                    "severity":1 if version == 2 else 2, "source":"Elisa-LSP",
                    "code":"E42", "range":{"start":{"line":0,"character":13},
                    "end":{"line":0,"character":15}}, "relatedInformation":[{
                    "location":{"uri":document["uri"], "range":{"start":{"line":0,"character":0},
                    "end":{"line":0,"character":3}}}, "message":"related declaration"}]}]
            }})
        elif method == "workspace/symbol":
            send({"jsonrpc":"2.0", "id":message["id"], "result":[{
                "name":"entry π", "kind":12, "containerName":"module",
                "location":{"uri":os.environ["LSP_WORKSPACE_SYMBOL_URI"],
                    "range":{"start":{"line":2,"character":4},
                             "end":{"line":2,"character":9}}}
            }]})
        elif method == "shutdown":
            send({"jsonrpc":"2.0", "id":message["id"], "result":None})
        elif method == "exit":
            break
    '''
)


def test_workspace_sync_diagnostics_edits_and_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="elisa-lsp-workspace-") as temporary:
        log_path = Path(temporary) / "opened.jsonl"
        env = dict(os.environ)
        env["LSP_WORKSPACE_OPEN_LOG"] = str(log_path)
        symbol_path = Path(temporary) / "symbol target.elisa"
        env["LSP_WORKSPACE_SYMBOL_URI"] = path_to_uri(symbol_path)
        supervisor = LspSupervisor(
            [sys.executable, "-u", "-c", SERVER],
            env=env,
            client_options={"startup_timeout": 2.0, "request_timeout": 2.0},
        )
        model = ProblemModel()
        workspace = LspWorkspace(supervisor, model)
        uri = path_to_uri(Path(temporary) / "unsaved π.elisa")
        original = 'let value = "😀"\nreturn 0\n'
        buffer = workspace.open_document(uri, original)
        assert buffer.sync_error is not None  # retained locally until the server starts
        try:
            workspace.start()
            assert buffer.sync_error is None and buffer.synced_revision == 1
            initial = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=2.0)
            assert initial["params"]["version"] == 1
            workspace.refresh_problems(uri)
            assert model.count == 1
            first = model.problems[0]
            assert first.message == "version 1"
            assert first.position_encoding == "utf-16"
            assert (first.start_byte, first.end_byte) == (13, 17)
            assert first.related_information[0].message == "related declaration"
            assert first.related_information[0].uri == uri
            symbols = workspace.workspace_symbols(uri, "entry")
            assert symbols == [{
                "name":"entry π", "kind":12, "containerName":"module",
                "location":{"uri":path_to_uri(symbol_path),
                    "range":{"start":{"line":2,"character":4},
                             "end":{"line":2,"character":9}}}
            }]

            workspace.apply_edit(uri, SourceEdit.text(13, 17, "🚀"))
            assert buffer.document.text == 'let value = "🚀"\nreturn 0\n'
            assert buffer.synced_revision == 2
            assert model.count == 0  # revision 1 is removed before new analysis arrives
            changed = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=2.0)
            assert changed["params"]["version"] == 2
            workspace.refresh_problems(uri)
            assert model.count == 1 and model.problems[0].severity is ProblemSeverity.ERROR
            assert (model.problems[0].start_byte, model.problems[0].end_byte) == (13, 17)

            assert workspace.undo(uri)
            assert buffer.document.revision == 3 and buffer.synced_revision == 3
            assert model.count == 0
            undone = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=2.0)
            assert undone["params"]["version"] == 3
            workspace.refresh_problems(uri)
            assert model.problems[0].message == "version 3"

            workspace.restart()
            replayed = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=2.0)
            assert replayed["params"]["version"] == 3
            assert buffer.document.text == original and buffer.synced_revision == 3
            workspace.refresh_problems(uri)
            assert model.problems[0].message == "version 3"
            opened = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            assert [item["version"] for item in opened] == [1, 2, 3, 3]
            assert opened[-1]["text"] == original

            assert workspace.close_document(uri)
            assert not workspace.close_document(uri)
            assert model.count == 0
            reopened = workspace.open_document(uri, original)
            assert reopened.lsp_version == 4
            reopened_diagnostic = supervisor.wait_notification(
                "textDocument/publishDiagnostics", timeout=2.0
            )
            assert reopened_diagnostic["params"]["version"] == 4
            workspace.refresh_problems(uri)
            assert model.count == 1 and model.problems[0].message == "version 4"
            assert workspace.find_all(uri, "return")
            assert workspace.replace_all(uri, "return", "yield") == 1
            assert reopened.document.text == 'let value = "😀"\nyield 0\n'
            assert reopened.lsp_version == 5 and model.count == 0
            replaced = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=2.0)
            assert replaced["params"]["version"] == 5
            workspace.refresh_problems(uri)
            assert model.problems[0].message == "version 5"
            assert workspace.indent_lines(uri, 1, 1, "\t")
            assert reopened.lsp_version == 6 and model.count == 0
            indented = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=2.0)
            assert indented["params"]["version"] == 6
            assert reopened.document.text == 'let value = "😀"\n\tyield 0\n'
            assert workspace.outdent_lines(uri, 1, 1, "\t")
            assert reopened.lsp_version == 7
            outdented = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=2.0)
            assert outdented["params"]["version"] == 7
            assert reopened.document.text == 'let value = "😀"\nyield 0\n'
            assert [json.loads(line)["version"] for line in log_path.read_text(encoding="utf-8").splitlines()] == [1, 2, 3, 3, 4, 5, 6, 7]
            assert workspace.close_document(uri) and model.count == 0
        finally:
            supervisor.abort()


def main() -> int:
    test_workspace_sync_diagnostics_edits_and_restart()
    print("lsp workspace service: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
