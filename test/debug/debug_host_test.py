#!/usr/bin/env python3
"""Exercise the native-shell debugger worker protocol with a fake DAP adapter."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "worker" / "debug" / "ide_debug_host.py"
ADAPTER = "#!/usr/bin/env python3\n" + textwrap.dedent(
    r'''
    import json, os, sys, time
    sequence = 1
    launch_seq = None
    def read_one():
        header = sys.stdin.buffer.readline()
        if not header:
            return None
        length = int(header.split(b":", 1)[1].strip())
        assert sys.stdin.buffer.readline() == b"\r\n"
        return json.loads(sys.stdin.buffer.read(length).decode())
    def send(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        sys.stdout.buffer.write(b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw)
        sys.stdout.buffer.flush()
    while True:
        request = read_one()
        if request is None:
            break
        command = request["command"]
        body = {}
        if command == "initialize":
            body = {"supportsConfigurationDoneRequest": True}
        elif command == "launch":
            launch_seq = request["seq"]
            send({"seq":sequence,"type":"event","event":"initialized","body":{}})
            sequence += 1
            continue
        elif command == "setBreakpoints":
            body = {"breakpoints":[{"verified":True,"line":item["line"]} for item in request["arguments"]["breakpoints"]]}
        elif command == "configurationDone":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True,"body":{}})
            sequence += 1
            send({"seq":sequence,"type":"event","event":"stopped","body":{"reason":"breakpoint","threadId":1}})
            sequence += 1
            send({"seq":sequence,"type":"response","request_seq":launch_seq,"command":"launch","success":True,"body":{}})
            sequence += 1
            continue
        elif command == "threads":
            body = {"threads":[{"id":1,"name":"main"},{"id":2,"name":"worker"}]}
        elif command == "stackTrace":
            if request["arguments"]["threadId"] == 2:
                body = {"stackFrames":[{"id":11,"name":"worker_entry","line":12,"column":1,"source":{"path":"sample.elisa"}}]}
            else:
                body = {"stackFrames":[{"id":7,"name":"main","line":4,"column":2,"source":{"path":"sample.elisa"}},{"id":9,"name":"helper","line":8,"column":3,"source":{"path":"sample.elisa"}}]}
        elif command == "scopes":
            references = {7:8, 9:9, 11:10}
            body = {"scopes":[{"name":"Locals","variablesReference":references[request["arguments"]["frameId"]]}]}
        elif command == "variables":
            values = {8:("answer","42"), 9:("label","inside helper"), 10:("worker_local","ready")}
            name, value = values[request["arguments"]["variablesReference"]]
            body = {"variables":[{"name":name,"value":value,"variablesReference":0}]}
        elif command == "continue":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True,"body":{}})
            sequence += 1
            if os.environ.get("ELISA_TEST_TERMINATE_AFTER_CONTINUE") == "1":
                send({"seq":sequence,"type":"event","event":"terminated","body":{}})
                sequence += 1
                continue
            time.sleep(0.10)
            send({"seq":sequence,"type":"event","event":"continued","body":{"threadId":1,"allThreadsContinued":True}})
            sequence += 1
            time.sleep(0.08)
            send({"seq":sequence,"type":"event","event":"stopped","body":{"reason":"step","threadId":1}})
            sequence += 1
            continue
        elif command == "disconnect":
            send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True,"body":{}})
            break
        send({"seq":sequence,"type":"response","request_seq":request["seq"],"command":command,"success":True,"body":body})
        sequence += 1
    '''
)


def read_response(process: subprocess.Popen[bytes]) -> str:
    assert process.stdout is not None
    chunks = bytearray()
    while True:
        byte = process.stdout.read(1)
        if not byte:
            process.wait(timeout=3.0)
            details = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise AssertionError(f"debug worker exited before completing a response: {details}")
        if byte == b"\x1e":
            return chunks.decode("utf-8")
        chunks.extend(byte)


def send(process: subprocess.Popen[bytes], command: str) -> str:
    assert process.stdin is not None
    process.stdin.write(command.encode("ascii") + b"\n")
    process.stdin.flush()
    return read_response(process)


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        workspace = folder / "ide"
        (workspace / "worker" / "debug").mkdir(parents=True)
        (workspace / "build").mkdir()
        (workspace / "src").symlink_to(ROOT / "src", target_is_directory=True)
        host_script = workspace / "worker" / "debug" / "ide_debug_host.py"
        shutil.copyfile(HOST, host_script)
        artifact = workspace / "build" / "ide_debug_host_test.edir"
        source = folder / "sample.elisa"
        source.write_text("def main() -> i64:\n    return 42\n", encoding="utf-8")
        adapter = folder / "fake_dap.py"
        adapter.write_text(ADAPTER, encoding="utf-8")
        adapter.chmod(0o700)
        artifact.write_bytes(b"bounded-test-edir")
        environment = dict(os.environ)
        environment["ELISA_DEBUGGER_DAP"] = os.fspath(adapter)
        process = subprocess.Popen(
            [sys.executable, "-u", os.fspath(host_script)],
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            start = "START 9 1 " + os.fsencode(artifact).hex() + " " + os.fsencode(source).hex()
            snapshot = send(process, start)
            assert "Debugger stopped (breakpoint)" in snapshot, snapshot
            assert "> #0 main at sample.elisa:4:2" in snapshot, snapshot
            assert "answer = 42" in snapshot, snapshot
            assert "Threads: *1 main; 2 worker" in snapshot, snapshot
            assert "Initial breakpoint verification: line 1 verified" in snapshot, snapshot
            selected_frame = send(process, "FRAME_NEXT")
            assert "  #0 main at sample.elisa:4:2" in selected_frame, selected_frame
            assert "> #1 helper at sample.elisa:8:3" in selected_frame, selected_frame
            assert "label = inside helper" in selected_frame and "answer = 42" not in selected_frame, selected_frame
            selected_thread = send(process, "THREAD_NEXT")
            assert "thread 2: worker" in selected_thread, selected_thread
            assert "Threads: 1 main; *2 worker" in selected_thread, selected_thread
            assert "> #0 worker_entry at sample.elisa:12:1" in selected_thread, selected_thread
            assert "worker_local = ready" in selected_thread, selected_thread
            returned_thread = send(process, "THREAD_PREV")
            assert "thread 1: main" in returned_thread, returned_thread
            wrapped_frame = send(process, "FRAME_PREV")
            assert "> #1 helper at sample.elisa:8:3" in wrapped_frame, wrapped_frame
            assert "label = inside helper" in wrapped_frame, wrapped_frame
            selected_by_index = send(process, "FRAME_SELECT 0")
            assert "> #0 main at sample.elisa:4:2" in selected_by_index, selected_by_index
            assert "answer = 42" in selected_by_index, selected_by_index
            assert "not in the current bounded stack" in send(process, "FRAME_SELECT 7")
            selected_by_index = send(process, "FRAME_SELECT 1")
            assert "> #1 helper at sample.elisa:8:3" in selected_by_index, selected_by_index
            assert "label = inside helper" in selected_by_index, selected_by_index
            breakpoint_update = send(process, "BREAKPOINTS 3,7")
            assert "Breakpoints: line 3 verified; line 7 verified" in breakpoint_update, breakpoint_update
            assert "label = inside helper" in breakpoint_update, breakpoint_update
            assert "duplicate lines" in send(process, "BREAKPOINTS 3,3")
            cleared = send(process, "BREAKPOINTS -")
            assert "Breakpoints: no breakpoints set" in cleared, cleared
            continued = send(process, "CONTINUE")
            assert "Debugger running" in continued, continued
            assert "Pause the debuggee before selecting a thread" in send(process, "THREAD_NEXT")
            final = ""
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and "Debugger stopped (step)" not in final:
                final = send(process, "POLL")
                if not final:
                    time.sleep(0.02)
            assert "Debugger stopped (step)" in final, final
            assert "> #0 main at sample.elisa:4:2" in final, final
            assert "answer = 42" in final, final
            assert send(process, "STOP") == "Debug session stopped."
            assert send(process, "SHUTDOWN") == "Debugger worker stopped."
            assert process.wait(timeout=3.0) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3.0)

        terminal_environment = dict(environment)
        terminal_environment["ELISA_TEST_TERMINATE_AFTER_CONTINUE"] = "1"
        terminal_process = subprocess.Popen(
            [sys.executable, "-u", os.fspath(host_script)],
            cwd=workspace,
            env=terminal_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            start = "START 10 1 " + os.fsencode(artifact).hex() + " " + os.fsencode(source).hex()
            assert "Debugger stopped (breakpoint)" in send(terminal_process, start)
            exited = send(terminal_process, "CONTINUE")
            assert exited == "Debuggee exited. Press Debug to launch it again.", exited
            assert send(terminal_process, "POLL") == ""
            assert send(terminal_process, "SHUTDOWN") == "Debugger worker stopped."
            assert terminal_process.wait(timeout=3.0) == 0
        finally:
            if terminal_process.poll() is None:
                terminal_process.kill()
                terminal_process.wait(timeout=3.0)
    print("debug host: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
