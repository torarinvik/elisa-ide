#!/usr/bin/env python3
"""DAP framing, capability, identity, event, and teardown contract tests."""

from __future__ import annotations

import os
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from debug import DapClient, DapSessionIdentity  # noqa: E402


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
        elif command == "disconnect":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True})
            sequence += 1
            break
    ''',
)


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
        assert client.close() == 0
    print("dap client: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
