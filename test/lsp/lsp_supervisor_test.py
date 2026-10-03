#!/usr/bin/env python3
"""Process-restart contract tests for the LSP workspace supervisor."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from lsp import LspError, LspSupervisor, SupervisorState, path_to_uri  # noqa: E402


RESTARTING_SERVER = textwrap.dedent(
    r'''
    import json, os, sys

    def read_message():
        header = sys.stdin.buffer.readline()
        if not header:
            return None
        if not header.startswith(b"Content-Length: ") or not header.endswith(b"\r\n"):
            return None
        length = int(header.split(b":", 1)[1].strip())
        if sys.stdin.buffer.readline() != b"\r\n":
            return None
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))

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
                "capabilities":{"positionEncoding":"utf-8", "textDocumentSync":1}
            }})
        elif method == "textDocument/didOpen":
            document = message["params"]["textDocument"]
            with open(os.environ["LSP_SUPERVISOR_OPEN_LOG"], "a", encoding="utf-8") as log:
                log.write(json.dumps(document, ensure_ascii=False) + "\n")
                log.flush()
            send({"jsonrpc":"2.0", "method":"textDocument/publishDiagnostics", "params":{
                "uri":document["uri"], "version":document["version"],
                "diagnostics":[{"message":"generation " + str(os.getpid()), "severity":2}]
            }})
        elif method == "test/crash":
            sys.stderr.write("intentional test server crash\n")
            sys.stderr.flush()
            os._exit(17)
        elif method == "shutdown":
            send({"jsonrpc":"2.0", "id":message["id"], "result":None})
        elif method == "exit":
            break
    ''',
)


def test_restart_replays_dirty_buffers_and_stops_at_limit() -> None:
    with tempfile.TemporaryDirectory(prefix="elisa-lsp-supervisor-") as temporary:
        log_path = Path(temporary) / "opened.jsonl"
        env = dict(os.environ)
        env["LSP_SUPERVISOR_OPEN_LOG"] = str(log_path)
        supervisor = LspSupervisor(
            [sys.executable, "-u", "-c", RESTARTING_SERVER],
            env=env,
            max_restarts=2,
            restart_window_seconds=30,
            client_options={"startup_timeout": 2.0, "request_timeout": 2.0},
        )
        uri = path_to_uri(Path(temporary) / "unsaved π.elisa")
        initially_opened_text = "def main() -> i64:\n    return 0\n"
        unsaved_text = "def main() -> i64:\n    let greeting = \"Grüße 😀\"\n    return 7\n"
        try:
            supervisor.start()
            supervisor.open_document(uri, initially_opened_text, version=6)
            first_notification = supervisor.wait_notification(
                "textDocument/publishDiagnostics", timeout=2.0
            )
            assert first_notification["params"]["version"] == 6
            assert supervisor.diagnostics.typed(uri)[0].version == 6
            assert supervisor.change_document(uri, unsaved_text, version=7) == 7

            # An orderly manual restart must also restore the current unsaved
            # buffer, and consumes one slot in the same rolling budget.
            supervisor.restart()
            assert supervisor.state == SupervisorState.RUNNING
            assert supervisor.generation == 2
            assert supervisor.restart_count == 1
            graceful_replay = supervisor.wait_notification(
                "textDocument/publishDiagnostics", timeout=2.0
            )
            assert graceful_replay["params"]["version"] == 7
            assert supervisor.diagnostics.typed(uri)[0].version == 7
            graceful_diagnostic_message = supervisor.diagnostics.typed(uri)[0].message

            try:
                supervisor.request("test/crash", timeout=2.0)
            except LspError:
                pass
            else:
                raise AssertionError("crashing LSP server returned a response")
            assert supervisor.state == SupervisorState.FAILED
            assert supervisor.last_failure is not None
            assert supervisor.last_failure.exit_code == 17
            assert "intentional test server crash" in supervisor.last_failure.stderr
            assert supervisor.diagnostics.typed(uri)[0].message == graceful_diagnostic_message
            assert supervisor.documents[uri].text == unsaved_text
            assert supervisor.documents[uri].version == 7

            supervisor.restart()
            assert supervisor.state == SupervisorState.RUNNING
            assert supervisor.generation == 3
            assert supervisor.restart_count == 2
            replayed_notification = supervisor.wait_notification(
                "textDocument/publishDiagnostics", timeout=2.0
            )
            assert replayed_notification["params"]["version"] == 7
            assert supervisor.diagnostics.typed(uri)[0].version == 7
            recovered_diagnostic_message = supervisor.diagnostics.typed(uri)[0].message
            assert recovered_diagnostic_message != graceful_diagnostic_message
            replayed_documents = [
                json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            assert replayed_documents == [
                {"uri": uri, "languageId": "elisa", "version": 6, "text": initially_opened_text},
                {"uri": uri, "languageId": "elisa", "version": 7, "text": unsaved_text},
                {"uri": uri, "languageId": "elisa", "version": 7, "text": unsaved_text},
            ]

            try:
                supervisor.request("test/crash", timeout=2.0)
            except LspError:
                pass
            else:
                raise AssertionError("second crashing LSP server returned a response")
            assert supervisor.last_failure is not None
            assert supervisor.diagnostics.typed(uri)[0].message == recovered_diagnostic_message
            try:
                supervisor.restart()
            except LspError as exc:
                assert "restart limit reached" in str(exc)
                assert "intentional test server crash" in str(exc)
            else:
                raise AssertionError("supervisor exceeded its configured restart limit")
            assert supervisor.documents[uri].text == unsaved_text
        finally:
            supervisor.abort()


def test_invalid_supervisor_configuration() -> None:
    for kwargs in (
        {"command": "python"},
        {"command": ["python"], "max_restarts": True},
        {"command": ["python"], "restart_window_seconds": float("nan")},
        {"command": ["python"], "client_options": {"cwd": "/tmp"}},
    ):
        try:
            command = kwargs.pop("command")
            LspSupervisor(command, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid supervisor configuration was accepted: {kwargs!r}")


def main() -> int:
    test_restart_replays_dirty_buffers_and_stops_at_limit()
    test_invalid_supervisor_configuration()
    print("lsp supervisor: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
