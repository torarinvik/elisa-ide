#!/usr/bin/env python3
"""Bounded line protocol between the native IDE shell and the Elisa DAP API."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(ROOT / "src"))

from debug.dap_session import (  # noqa: E402
    DebugSessionError,
    DebugSessionState,
    ElisaDebugSession,
    resolve_debug_adapter,
)
from toolchain.resolver import ToolchainError  # noqa: E402

MAX_COMMAND_BYTES = 16 * 1024
MAX_PATH_BYTES = 1024
MAX_RESPONSE_BYTES = 8192
MAX_THREADS = 16
MAX_FRAMES = 6
MAX_VARIABLES = 24
MAX_BREAKPOINTS = 256
RESPONSE_END = b"\x1e"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class DebugHost:
    def __init__(self) -> None:
        self.session: ElisaDebugSession | None = None
        self.thread_id = 1
        self.frame_index = 0
        self.stop_reason = "entry"
        self.source_path: Path | None = None

    @staticmethod
    def _decode_hex(value: str, label: str, limit: int) -> str:
        if len(value) % 2 or len(value) > limit * 2:
            raise ValueError(f"{label} has an invalid encoded length")
        try:
            decoded = bytes.fromhex(value).decode("utf-8", errors="strict")
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"{label} is not valid UTF-8 hex") from exc
        if not decoded or "\x00" in decoded:
            raise ValueError(f"{label} is empty or contains a NUL byte")
        return decoded

    @staticmethod
    def _display(value: Any, limit: int = 300) -> str:
        text = _CONTROL.sub(" ", str(value))
        return text[:limit]

    @staticmethod
    def _artifact_path(value: str) -> Path:
        path = Path(value).expanduser()
        path = path if path.is_absolute() else ROOT / path
        resolved = path.resolve(strict=True)
        build_root = (ROOT / "build").resolve(strict=True)
        try:
            resolved.relative_to(build_root)
        except ValueError as exc:
            raise ValueError("debug EDIR artifact must be inside this IDE's build directory") from exc
        return resolved

    @staticmethod
    def _source_path(value: str) -> Path:
        source = Path(value).expanduser().resolve(strict=True)
        if source.suffix != ".elisa" or not source.is_file():
            raise ValueError("debug source must be a saved .elisa file")
        if len(os.fspath(source).encode("utf-8")) > MAX_PATH_BYTES:
            raise ValueError("debug source path exceeds the DAP limit")
        return source

    @staticmethod
    def _breakpoint_lines(value: str) -> list[int]:
        if value == "-":
            return []
        fields = value.split(",")
        if not fields or len(fields) > MAX_BREAKPOINTS or any(not field.isdecimal() for field in fields):
            raise ValueError("breakpoint list must contain at most 256 positive line numbers")
        lines = [int(field, 10) for field in fields]
        if any(line < 1 or line > 100_000_000 for line in lines):
            raise ValueError("breakpoint lines must be between 1 and 100000000")
        if len(set(lines)) != len(lines):
            raise ValueError("breakpoint list contains duplicate lines")
        return lines

    def _consume_events(self, events: tuple[Mapping[str, Any], ...]) -> None:
        for event in events:
            if event.get("event") == "stopped":
                body = event.get("body", {})
                if isinstance(body, dict):
                    thread_id = body.get("threadId")
                    if type(thread_id) is int and thread_id > 0:
                        self.thread_id = thread_id
                        self.frame_index = 0
                    reason = body.get("reason")
                    if isinstance(reason, str) and reason:
                        self.stop_reason = self._display(reason, 80)

    def _snapshot(self) -> str:
        session = self.session
        if session is None:
            return "Debugger is not running."
        if session.state == DebugSessionState.FAILED:
            return "Debugger failed: " + self._display(session.failure or "unknown DAP failure", 1200)
        if session.state == DebugSessionState.EXITED:
            return "Debuggee exited. Press Debug to launch it again."
        if session.state == DebugSessionState.RUNNING:
            return "Debugger running. Continue, pause, or step when the target stops."
        if session.state != DebugSessionState.STOPPED:
            return f"Debugger {session.state.value}. Waiting for the first stop."

        threads = session.threads()[:MAX_THREADS]
        selected_thread_name = "<unknown>"
        thread_labels = []
        for thread in threads:
            thread_id = thread.get("id")
            name = self._display(thread.get("name", "<unnamed>"), 80)
            if type(thread_id) is not int or thread_id <= 0:
                continue
            if thread_id == self.thread_id:
                selected_thread_name = name
            marker = "*" if thread_id == self.thread_id else ""
            thread_labels.append(f"{marker}{thread_id} {name}")
        rows = [
            f"Debugger stopped ({self.stop_reason}) | thread {self.thread_id}: {selected_thread_name}",
            "  Threads: " + ("; ".join(thread_labels) if thread_labels else "none reported"),
        ]
        frames = session.stack_trace(self.thread_id, levels=MAX_FRAMES)
        if not frames:
            rows.append("  Stack is empty.")
            return "\n".join(rows)
        if self.frame_index >= len(frames):
            self.frame_index = len(frames) - 1
        for index, frame in enumerate(frames[:MAX_FRAMES]):
            name = self._display(frame.get("name", "<anonymous>"), 120)
            line = frame.get("line", 0)
            column = frame.get("column", 0)
            source = frame.get("source", {})
            source_path = source.get("path", "") if isinstance(source, dict) else ""
            location = self._display(source_path, 300)
            if location:
                location += ":"
            location += str(line if type(line) is int else 0)
            if type(column) is int and column > 0:
                location += ":" + str(column)
            marker = ">" if index == self.frame_index else " "
            rows.append(f"  {marker} #{index} {name} at {location}")
            if index == self.frame_index:
                try:
                    scopes = session.scopes(frame.get("id")) if type(frame.get("id")) is int else []
                    emitted = 0
                    for scope in scopes:
                        reference = scope.get("variablesReference")
                        if type(reference) is not int or reference <= 0:
                            continue
                        variables = session.variables(reference)
                        for variable in variables:
                            if emitted >= MAX_VARIABLES:
                                break
                            variable_name = self._display(variable.get("name", "?"), 80)
                            variable_value = self._display(variable.get("value", "<unavailable>"), 200)
                            rows.append(f"      {variable_name} = {variable_value}")
                            emitted += 1
                        if emitted >= MAX_VARIABLES:
                            break
                    if emitted == 0:
                        rows.append("      No local variables available.")
                except (DebugSessionError, ValueError) as exc:
                    rows.append("      Locals unavailable: " + self._display(exc, 200))
        return "\n".join(rows)

    def _cycle_thread(self, direction: int) -> str:
        session = self.session
        if session is None:
            raise ValueError("no debug session is active")
        if session.state != DebugSessionState.STOPPED:
            return "Pause the debuggee before selecting a thread."
        threads = [
            thread for thread in session.threads()
            if type(thread.get("id")) is int and thread["id"] > 0
        ][:MAX_THREADS]
        if not threads:
            return "Debugger stopped; the adapter reported no selectable threads."
        current = next(
            (index for index, thread in enumerate(threads) if thread["id"] == self.thread_id),
            0,
        )
        selected = (current + direction) % len(threads)
        self.thread_id = threads[selected]["id"]
        self.frame_index = 0
        self._consume_events(session.poll_events(timeout=0.0))
        return self._snapshot_and_cleanup()

    def _cycle_frame(self, direction: int) -> str:
        session = self.session
        if session is None:
            raise ValueError("no debug session is active")
        if session.state != DebugSessionState.STOPPED:
            return "Pause the debuggee before selecting a stack frame."
        frames = session.stack_trace(self.thread_id, levels=MAX_FRAMES)
        if not frames:
            self.frame_index = 0
            return "Debugger stopped; the selected thread has no stack frames."
        self.frame_index = (self.frame_index + direction) % min(len(frames), MAX_FRAMES)
        self._consume_events(session.poll_events(timeout=0.0))
        return self._snapshot_and_cleanup()

    def _select_frame(self, index: int) -> str:
        session = self.session
        if session is None:
            raise ValueError("no debug session is active")
        if session.state != DebugSessionState.STOPPED:
            return "Pause the debuggee before selecting a stack frame."
        frames = session.stack_trace(self.thread_id, levels=MAX_FRAMES)
        if index < 0 or index >= min(len(frames), MAX_FRAMES):
            return "The requested stack frame is not in the current bounded stack snapshot."
        self.frame_index = index
        self._consume_events(session.poll_events(timeout=0.0))
        return self._snapshot_and_cleanup()

    def _snapshot_and_cleanup(self) -> str:
        """Return the last useful terminal state, then reap an exited adapter."""
        session = self.session
        if session is not None and session.state == DebugSessionState.EXITED:
            self._close_session()
            return "Debuggee exited. Press Debug to launch it again."
        snapshot = self._snapshot()
        session = self.session
        if session is not None and session.state == DebugSessionState.EXITED:
            self._close_session()
            return "Debuggee exited. Press Debug to launch it again."
        return snapshot

    def _emit(self, text: str) -> None:
        data = text.encode("utf-8", errors="replace")[:MAX_RESPONSE_BYTES]
        sys.stdout.buffer.write(data + RESPONSE_END)
        sys.stdout.buffer.flush()

    def _start(self, fields: list[str]) -> str:
        if len(fields) not in (5, 6):
            raise ValueError("START requires revision, line, artifact path, source path, and optional breakpoint list")
        revision = int(fields[1], 10)
        line = int(fields[2], 10)
        if revision < 0 or line < 1 or line > 100_000_000:
            raise ValueError("debug revision or initial breakpoint line is out of range")
        artifact = self._artifact_path(self._decode_hex(fields[3], "artifact path", MAX_PATH_BYTES))
        source = self._source_path(self._decode_hex(fields[4], "source path", MAX_PATH_BYTES))
        lines = self._breakpoint_lines(fields[5]) if len(fields) == 6 and fields[5] != "-" else [line]
        self._close_session()
        adapter = resolve_debug_adapter(
            ROOT,
            trusted_nearby=(ROOT.parent / "Elisa-debugger" / "build" / "elisa-debugger-dap-server",),
        )
        session = ElisaDebugSession(
            [os.fspath(adapter.path)],
            artifact,
            revision,
            source_path_root=source.parent,
            adapter_cwd=ROOT,
        )
        self.session = session
        self.source_path = source
        self.thread_id = 1
        self.frame_index = 0
        self.stop_reason = "entry"
        session.start({source: lines})
        self._consume_events(session.poll_events(timeout=0.05))
        breakpoints = session.breakpoints.get(os.fspath(source), ())
        notice = ""
        if breakpoints:
            notice = "\nInitial breakpoint verification: " + self._breakpoint_summary(breakpoints)
        return self._snapshot_and_cleanup() + notice

    def _breakpoint_summary(self, results: tuple[Any, ...]) -> str:
        parts = []
        for result in results[:MAX_BREAKPOINTS]:
            state = "verified" if result.verified else "unverified"
            actual_line = result.line if result.line is not None else result.requested_line
            detail = f"line {actual_line} {state}"
            if result.message:
                detail += ": " + self._display(result.message, 160)
            parts.append(detail)
        return "; ".join(parts) if parts else "no breakpoints set"

    def handle(self, raw: bytes) -> tuple[str, bool]:
        try:
            command = raw.decode("ascii", errors="strict").strip()
            fields = command.split(" ") if command else []
            if not fields:
                return "", False
            verb = fields[0].upper()
            if verb == "START":
                return self._start(fields), False
            if verb == "POLL":
                if self.session is None:
                    return "", False
                events = self.session.poll_events(timeout=0.0)
                self._consume_events(events)
                return (self._snapshot_and_cleanup() if events else ""), False
            if verb == "BREAKPOINTS":
                if len(fields) != 2 or self.session is None or self.source_path is None:
                    raise ValueError("BREAKPOINTS requires an active debug session and a line list")
                lines = self._breakpoint_lines(fields[1])
                results = self.session.set_breakpoints(self.source_path, lines)
                self._consume_events(self.session.poll_events(timeout=0.0))
                return "Breakpoints: " + self._breakpoint_summary(results) + "\n" + self._snapshot_and_cleanup(), False
            if verb in ("THREAD_NEXT", "THREAD_PREV"):
                if len(fields) != 1:
                    raise ValueError(f"{verb} takes no arguments")
                return self._cycle_thread(1 if verb == "THREAD_NEXT" else -1), False
            if verb in ("FRAME_NEXT", "FRAME_PREV"):
                if len(fields) != 1:
                    raise ValueError(f"{verb} takes no arguments")
                return self._cycle_frame(1 if verb == "FRAME_NEXT" else -1), False
            if verb == "FRAME_SELECT":
                if len(fields) != 2 or not fields[1].isdecimal():
                    raise ValueError("FRAME_SELECT requires one nonnegative stack index")
                index = int(fields[1], 10)
                return self._select_frame(index), False
            if verb in ("CONTINUE", "NEXT", "PAUSE"):
                if len(fields) != 1 or self.session is None:
                    raise ValueError(f"{verb} requires an active debug session")
                if verb == "CONTINUE":
                    self.session.continue_execution(self.thread_id)
                elif verb == "NEXT":
                    self.session.step("next", self.thread_id)
                else:
                    self.session.pause(self.thread_id)
                self._consume_events(self.session.poll_events(timeout=0.05))
                return self._snapshot_and_cleanup(), False
            if verb == "STOP":
                if len(fields) != 1:
                    raise ValueError("STOP takes no arguments")
                self._close_session()
                return "Debug session stopped.", False
            if verb == "SHUTDOWN":
                self._close_session()
                return "Debugger worker stopped.", True
            raise ValueError("unknown debugger worker command")
        except (DebugSessionError, ToolchainError, OSError, ValueError) as exc:
            if self.session is not None and self.session.state == DebugSessionState.FAILED:
                self._close_session()
            return "Debugger error: " + self._display(exc, 1200), False

    def _close_session(self) -> None:
        session, self.session = self.session, None
        if session is None:
            self.source_path = None
            return
        try:
            session.close()
        except (DebugSessionError, OSError):
            try:
                session.client.abort()
            except Exception:
                pass
        self.source_path = None


def main() -> int:
    host = DebugHost()
    while True:
        raw = sys.stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
        if not raw:
            break
        if len(raw) > MAX_COMMAND_BYTES:
            host._emit("Debugger error: command exceeds the protocol size limit")
            continue
        response, should_exit = host.handle(raw)
        host._emit(response)
        if should_exit:
            return 0
    host._close_session()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
