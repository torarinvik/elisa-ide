#!/usr/bin/env python3
"""Managed debug-session identity, launch, inspection, and capability tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from debug.dap_session import (  # noqa: E402
    DebugSessionError,
    DebugSessionState,
    ElisaDebugSession,
    inspect_debug_artifact,
)


FAKE_ADAPTER = textwrap.dedent(
    r'''
    import json, os, sys, time

    def read_one():
        header = sys.stdin.buffer.readline()
        if not header:
            return None
        if not header.lower().startswith(b"content-length:"):
            return None
        length = int(header.split(b":", 1)[1].strip())
        if sys.stdin.buffer.readline() != b"\r\n":
            return None
        return json.loads(sys.stdin.buffer.read(length).decode())

    def send(value):
        body = json.dumps(value, separators=(",", ":")).encode()
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sys.stdout.buffer.flush()

    sequence = 1
    launch_request_sequence = None
    while True:
        request = read_one()
        if request is None:
            break
        command = request.get("command")
        arguments = request.get("arguments", {})
        body = {}
        if command == "initialize":
            body = {
                "supportsConfigurationDoneRequest": True,
                "supportsEvaluateForHovers": True,
                "supportsStepBack": True,
                "supportsTerminateRequest": True,
                "supportsSetVariable": False,
            }
            mutate_path = os.environ.get("ELISA_DAP_TEST_MUTATE_ARTIFACT")
            if mutate_path:
                with open(mutate_path, "wb") as artifact:
                    artifact.write(b"changed during adapter initialization")
        elif command == "launch":
            launch_request_sequence = request["seq"]
            send({"seq": sequence, "type": "event", "event": "initialized", "body": {}})
            sequence += 1
            continue
        elif command == "setBreakpoints":
            body = {"breakpoints": [
                {"verified": True, "line": item["line"],
                 **({"column": item["column"]} if "column" in item else {})}
                for item in arguments.get("breakpoints", [])
            ]}
        elif command == "threads":
            body = {"threads": [{"id": 1, "name": "main"}]}
        elif command == "stackTrace":
            body = {"stackFrames": [{"id": 7, "name": "main", "line": 4}]}
        elif command == "scopes":
            body = {"scopes": [{"name": "Locals", "variablesReference": 8}]}
        elif command == "variables":
            body = {"variables": [{"name": "answer", "value": "42", "variablesReference": 0}]}
        elif command == "evaluate":
            body = {"result": "42", "variablesReference": 0}
        elif command == "configurationDone":
            send({"seq": sequence, "type": "response", "request_seq": request["seq"],
                  "command": command, "success": True, "body": body})
            sequence += 1
            time.sleep(0.08)
            send({"seq": sequence, "type": "event", "event": "stopped",
                  "body": {"reason": "entry", "threadId": 1}})
            sequence += 1
            send({"seq": sequence, "type": "response", "request_seq": launch_request_sequence,
                  "command": "launch", "success": True, "body": {}})
            sequence += 1
            continue
        elif command == "continue":
            body = {"allThreadsContinued": True}
        elif command == "pause":
            body = {}
        elif command == "terminate":
            body = {}
        elif command == "disconnect":
            send({"seq": sequence, "type": "response", "request_seq": request["seq"],
                  "command": command, "success": True})
            sequence += 1
            break

        send({"seq": sequence, "type": "response", "request_seq": request["seq"],
              "command": command, "success": True, "body": body})
        sequence += 1
        if command in ("continue", "stepBack"):
            send({"seq": sequence, "type": "event", "event": "continued",
                  "body": {"threadId": 1, "allThreadsContinued": True}})
            sequence += 1
        elif command == "pause":
            send({"seq": sequence, "type": "event", "event": "stopped",
                  "body": {"reason": "pause", "threadId": 1}})
            sequence += 1
        elif command == "terminate":
            send({"seq": sequence, "type": "event", "event": "terminated", "body": {}})
            sequence += 1
    ''',
)


def expect_session_error(action, message: str) -> None:
    try:
        action()
    except DebugSessionError:
        return
    raise AssertionError(message)


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_root = root / "workspace"
        source_root.mkdir()
        source = source_root / "main.elisa"
        source.write_text("main = 42\n", encoding="utf-8")
        artifact_path = root / "build" / "main.edir"
        artifact_path.parent.mkdir()
        artifact_path.write_bytes(b"verified-edir-artifact")
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

        artifact = inspect_debug_artifact(artifact_path, expected_sha256=digest)
        assert artifact.sha256 == digest and artifact.size == len(b"verified-edir-artifact")
        expect_session_error(
            lambda: inspect_debug_artifact(artifact_path, expected_sha256="0" * 64),
            "artifact hash mismatch was accepted",
        )

        session = ElisaDebugSession(
            [sys.executable, "-u", "-c", FAKE_ADAPTER],
            artifact_path,
            11,
            expected_artifact_sha256=digest,
            source_path_root=source_root,
            request_timeout=2.0,
        )
        session.start({source: [4]})
        assert session.state == DebugSessionState.STOPPED
        assert session.identity.source_revision == 11
        assert session.identity.artifact_sha256 == digest
        assert session.identity.edir_identity == f"sha256:{digest}"
        assert session.breakpoints[str(source.resolve())][0].verified
        assert session.wait_event("stopped", timeout=2.0)["event"] == "stopped"
        assert session.state == DebugSessionState.STOPPED

        unicode_source = "a😀e\u0301\r\n𝄞x\n"
        unicode_path = source_root / "unicode.elisa"
        unicode_path.write_text(unicode_source, encoding="utf-8")
        unicode_breakpoints = session.set_edir_breakpoints(
            unicode_path, unicode_source, ((1, 6), (2, 5))
        )
        assert [(item.requested_line, item.requested_byte_column, item.dap_column)
                for item in unicode_breakpoints] == [(1, 6, 3), (2, 5, 2)]

        expect_session_error(
            lambda: session.set_breakpoints(root / "outside.elisa", [1]),
            "breakpoint outside configured source root was accepted",
        )
        assert session.threads()[0]["id"] == 1
        assert session.stack_trace(1)[0]["name"] == "main"
        assert session.scopes(7)[0]["variablesReference"] == 8
        assert session.variables(8)[0]["value"] == "42"
        assert session.evaluate("answer", frame_id=7)["result"] == "42"
        session.continue_execution(1)
        assert session.wait_event("continued", timeout=2.0)["event"] == "continued"
        assert session.state == DebugSessionState.RUNNING
        session.step("stepBack", 1)
        assert session.wait_event("continued", timeout=2.0)["event"] == "continued"
        assert session.state == DebugSessionState.RUNNING
        session.pause(1)
        assert session.wait_event("stopped", timeout=2.0)["event"] == "stopped"
        assert session.state == DebugSessionState.STOPPED
        session.terminate()
        assert session.wait_event("terminated", timeout=2.0)["event"] == "terminated"
        assert session.state == DebugSessionState.EXITED
        assert session.close() == 0
        assert session.state == DebugSessionState.CLOSED

        unsupported = ElisaDebugSession(
            [sys.executable, "-u", "-c", FAKE_ADAPTER.replace('"supportsEvaluateForHovers": True,', '"supportsEvaluateForHovers": False,')],
            artifact_path,
            12,
            source_path_root=source_root,
            request_timeout=2.0,
        )
        unsupported.start()
        expect_session_error(
            lambda: unsupported.evaluate("answer"),
            "unsupported expression evaluation was sent to the adapter",
        )
        unsupported.close()

        mutating_env = dict(os.environ)
        mutating_env["ELISA_DAP_TEST_MUTATE_ARTIFACT"] = os.fspath(artifact_path)
        stale = ElisaDebugSession(
            [sys.executable, "-u", "-c", FAKE_ADAPTER],
            artifact_path,
            13,
            source_path_root=source_root,
            adapter_env=mutating_env,
            request_timeout=2.0,
        )
        expect_session_error(stale.start, "changed EDIR artifact was launched")
        assert stale.state == DebugSessionState.FAILED
        assert stale.client.process is not None and stale.client.process.poll() is not None

    print("dap session: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
