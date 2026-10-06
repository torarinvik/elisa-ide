#!/usr/bin/env python3
"""DAP framing, capability, identity, event, and teardown contract tests."""

from __future__ import annotations

import os
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from debug import DapClient, DapError, DapSessionIdentity  # noqa: E402


FAKE_ADAPTER = textwrap.dedent(
    r'''
    import json, sys
    def read_one():
        header = sys.stdin.buffer.readline()
        if not header:
            return None
        length = int(header.split(b":", 1)[1].strip())
        if sys.stdin.buffer.readline() != b"\r\n":
            return None
        return json.loads(sys.stdin.buffer.read(length).decode())
    def send(value, split=False):
        body = json.dumps(value, separators=(",", ":")).encode()
        raw = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        if split:
            for byte in raw:
                sys.stdout.buffer.write(bytes([byte]))
                sys.stdout.buffer.flush()
        else:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
    sequence = 1
    while True:
        request = read_one()
        if request is None:
            break
        command = request.get("command")
        if command == "initialize":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":"initialize","success":True,"body":{"supportsConfigurationDoneRequest":True,"supportsSetVariable":False}}, split=True)
            sequence += 1
            send({"seq":sequence,"type":"event","event":"initialized","body":{}})
            sequence += 1
        elif command == "setBreakpoints":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True,"body":{"breakpoints":[{"verified":True,"line":3}]}})
            sequence += 1
        elif command == "continue":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True,"body":{}})
            sequence += 1
            import time
            time.sleep(0.05)
            send({"seq":sequence,"type":"event","event":"continued","body":{"threadId":1}})
            sequence += 1
        elif command == "disconnect":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True})
            sequence += 1
            break
    ''',
)

INVALID_ADAPTER = textwrap.dedent(
    r'''
    import json, sys, time
    mode = sys.argv[1]
    header = sys.stdin.buffer.readline()
    length = int(header.split(b":", 1)[1].strip())
    sys.stdin.buffer.readline()
    request = json.loads(sys.stdin.buffer.read(length).decode())
    if mode == "timeout":
        time.sleep(10)
    elif mode == "malformed-frame":
        sys.stdout.buffer.write(b"Content-Length: nope\r\n\r\n")
    elif mode == "malformed-event":
        body = json.dumps({"seq":1,"type":"response","request_seq":request["seq"],
            "command":request["command"],"success":True,"body":{}}).encode()
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sys.stdout.buffer.flush()
        time.sleep(0.05)
        body = json.dumps({"seq":2,"type":"event"}).encode()
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    else:
        if mode == "oversized":
            body = b"{" + (b" " * 100)
            sys.stdout.buffer.write(b"Content-Length: 101\r\n\r\n" + body)
        else:
            command = "different" if mode == "command-mismatch" else request["command"]
            request_seq = 999 if mode == "unknown-sequence" else request["seq"]
            body = json.dumps({"seq":1,"type":"response","request_seq":request_seq,
                "command":command,"success":True,"body":{}}).encode()
            sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()
    time.sleep(10)
    ''',
)


def expect_adapter_failure(mode: str, *, max_body_bytes: int = 1024, timeout: float = 0.5) -> None:
    identity = DapSessionIdentity("build/app", "b" * 64, 1, "failure-test")
    client = DapClient(
        [sys.executable, "-u", "-c", INVALID_ADAPTER, mode],
        identity,
        request_timeout=timeout,
        max_body_bytes=max_body_bytes,
    )
    client.start()
    try:
        client.request("ping")
    except DapError:
        pass
    else:
        client.abort()
        raise AssertionError(f"adapter mode {mode!r} was accepted")
    assert client.process is not None and client.process.poll() is not None, mode


def expect_malformed_event_failure() -> None:
    identity = DapSessionIdentity("build/app", "c" * 64, 1, "event-test")
    client = DapClient(
        [sys.executable, "-u", "-c", INVALID_ADAPTER, "malformed-event"],
        identity,
        request_timeout=1.0,
    )
    client.start()
    assert client.initialize()["success"] is True
    try:
        client.wait_event("initialized")
    except DapError:
        pass
    else:
        client.abort()
        raise AssertionError("malformed event envelope was accepted")
    assert client.process is not None and client.process.poll() is not None


def main() -> int:
    identity = DapSessionIdentity("build/app", "a" * 64, 7, "edir-7")
    with DapClient([sys.executable, "-u", "-c", FAKE_ADAPTER], identity, request_timeout=2.0) as client:
        initialized = client.initialize()
        assert initialized["success"] is True
        assert client.identity.source_revision == 7
        assert client.supports("supportsConfigurationDoneRequest")
        assert client.wait_event("initialized")["event"] == "initialized"
        response = client.request("setBreakpoints", {"source": {"path": "main.elisa"}, "breakpoints": [{"line": 3}]})
        assert response["success"] is True and response["body"]["breakpoints"][0]["verified"] is True
        assert client.request("continue")["success"] is True
        assert client.poll_available(timeout=1.0) == 1
        assert client.events.pop(0)["event"] == "continued"
        assert client.close() == 0
    expect_adapter_failure("malformed-frame")
    expect_adapter_failure("unknown-sequence")
    expect_adapter_failure("command-mismatch")
    expect_adapter_failure("oversized", max_body_bytes=64)
    expect_adapter_failure("timeout", timeout=0.05)
    expect_malformed_event_failure()
    print("dap client: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
