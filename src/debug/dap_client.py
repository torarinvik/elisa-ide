"""Bounded Debug Adapter Protocol transport for Elisa IDE.

The adapter is intentionally transport-only: it never invents debugger
features.  Commands are enabled from the adapter's initialize capabilities and
every session is bound to the exact build/EDIR/source identity supplied by the
caller.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from lsp.lsp_client import FrameDecoder, FrameError, frame


class DapError(RuntimeError):
    """A DAP process, deadline, framing, or lifecycle failure."""


class DapProtocolError(DapError):
    """A DAP message violates the adapter protocol contract."""


@dataclass(frozen=True)
class DapSessionIdentity:
    artifact_path: str
    artifact_sha256: str
    source_revision: int
    edir_identity: str


class DapClient:
    """A strict, deadline-bounded DAP stdio client."""

    def __init__(
        self,
        command: Iterable[str],
        identity: DapSessionIdentity,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        request_timeout: float = 5.0,
        teardown_timeout: float = 3.0,
        max_header_bytes: int = 8192,
        max_body_bytes: int = 16 << 20,
        max_stream_bytes: int = 64 << 20,
        max_messages: int = 100_000,
        stderr_limit: int = 64 << 10,
    ) -> None:
        self.command = list(command)
        if not self.command:
            raise ValueError("DAP command must not be empty")
        if request_timeout <= 0 or teardown_timeout <= 0:
            raise ValueError("DAP timeouts must be positive")
        if max_stream_bytes < 1 or max_messages < 1 or stderr_limit < 0:
            raise ValueError("DAP limits are invalid")
        self.identity = identity
        self.cwd = os.fspath(cwd) if cwd is not None else None
        self.env = dict(env) if env is not None else None
        self.request_timeout = request_timeout
        self.teardown_timeout = teardown_timeout
        self.max_stream_bytes = max_stream_bytes
        self.max_messages = max_messages
        self.stderr_limit = stderr_limit
        self.decoder = FrameDecoder(max_header_bytes=max_header_bytes, max_body_bytes=max_body_bytes)
        self.process: subprocess.Popen[bytes] | None = None
        self._next_seq = 1
        self._pending: dict[int, str] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.capabilities: Mapping[str, Any] = {}
        self.initialized = False
        self._stream_bytes = 0
        self._message_count = 0
        self._stdout_eof = False
        self._closed = False
        self._stderr = bytearray()
        self._stderr_truncated = False

    def __enter__(self) -> "DapClient":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self.abort()
        elif not self._closed:
            self.close()
        return False

    def start(self) -> None:
        if self.process is not None:
            raise DapError("DAP client is already started")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise DapError(f"could not start DAP adapter {self.command!r}: {exc}") from exc
        assert self.process.stderr is not None
        # stderr is kept bounded without sharing the DAP stdout protocol pipe.
        import threading

        threading.Thread(target=self._drain_stderr, daemon=True).start()
        time.sleep(0.01)
        if self.process.poll() is not None:
            status = self.process.returncode
            details = self.stderr_text()[:2000]
            self.abort()
            raise DapError(f"DAP adapter exited during startup ({status}); stderr={details!r}")

    def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                chunk = os.read(self.process.stderr.fileno(), 4096)
                if not chunk:
                    return
                self._stderr.extend(chunk)
                if len(self._stderr) > self.stderr_limit:
                    del self._stderr[: len(self._stderr) - self.stderr_limit]
                    self._stderr_truncated = True
        except OSError:
            return

    def stderr_text(self) -> str:
        prefix = "[earlier stderr truncated] " if self._stderr_truncated else ""
        return prefix + bytes(self._stderr).decode("utf-8", errors="replace")

    def _ensure_running(self) -> subprocess.Popen[bytes]:
        if self.process is None:
            raise DapError("DAP client is not started")
        if self._closed:
            raise DapError("DAP client is closed")
        status = self.process.poll()
        if status is not None:
            raise DapError(f"DAP adapter exited with status {status}; stderr={self.stderr_text()[:2000]!r}")
        return self.process

    def _write_all(self, payload: bytes, deadline: float) -> None:
        process = self._ensure_running()
        assert process.stdin is not None
        fd = process.stdin.fileno()
        offset = 0
        while offset < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.abort()
                raise DapError("timed out writing to DAP adapter; subprocess killed and reaped")
            try:
                _, writable, _ = select.select([], [fd], [], remaining)
            except InterruptedError:
                continue
            if not writable:
                self.abort()
                raise DapError("timed out waiting for writable DAP stdin")
            try:
                count = os.write(fd, payload[offset:])
            except (BrokenPipeError, OSError) as exc:
                self.abort()
                raise DapError(f"DAP stdin closed: {exc}") from exc
            if count <= 0:
                self.abort()
                raise DapError("DAP stdin accepted no bytes")
            offset += count

    def send(self, message: Mapping[str, Any], *, timeout: float | None = None) -> None:
        duration = self.request_timeout if timeout is None else timeout
        if duration <= 0:
            raise ValueError("timeout must be positive")
        self._write_all(frame(dict(message)), time.monotonic() + duration)

    def _read(self, deadline: float) -> list[dict[str, Any]]:
        process = self._ensure_running()
        assert process.stdout is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DapError("timed out waiting for DAP output")
        try:
            readable, _, _ = select.select([process.stdout.fileno()], [], [], remaining)
        except InterruptedError:
            return []
        if not readable:
            raise DapError("timed out waiting for DAP output")
        chunk = os.read(process.stdout.fileno(), 8192)
        if not chunk:
            self._stdout_eof = True
            self.decoder.finish()
            return []
        self._stream_bytes += len(chunk)
        if self._stream_bytes > self.max_stream_bytes:
            raise DapProtocolError(f"DAP stdout exceeds {self.max_stream_bytes} bytes")
        messages: list[dict[str, Any]] = []
        try:
            decoded = self.decoder.feed(chunk)
        except FrameError as exc:
            self.abort()
            raise DapProtocolError(str(exc)) from exc
        for item in decoded:
            self._record(item.message)
            messages.append(item.message)
        return messages

    def _record(self, message: dict[str, Any]) -> None:
        self._message_count += 1
        if self._message_count > self.max_messages:
            raise DapProtocolError(f"DAP message count exceeds {self.max_messages}")
        if not isinstance(message.get("type"), str):
            raise DapProtocolError("DAP message must contain a string type")
        seq = message.get("seq")
        if type(seq) is not int or seq < 0:
            raise DapProtocolError("DAP message seq must be a nonnegative integer")
        kind = message["type"]
        if kind == "response":
            request_seq = message.get("request_seq")
            if type(request_seq) is not int or request_seq not in self._pending:
                raise DapProtocolError(f"DAP response arrived for unknown request_seq {request_seq!r}")
            if not isinstance(message.get("command"), str) or type(message.get("success")) is not bool:
                raise DapProtocolError("DAP response has an invalid shape")
            self._responses[request_seq] = message
        elif kind == "event":
            if not isinstance(message.get("event"), str):
                raise DapProtocolError("DAP event must contain a string event name")
            self.events.append(message)
        elif kind == "request":
            if not isinstance(message.get("command"), str):
                raise DapProtocolError("DAP server request must contain a command")
        else:
            raise DapProtocolError(f"unknown DAP message type {kind!r}")
        self.messages.append(message)

    def request(self, command: str, arguments: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        duration = self.request_timeout if timeout is None else timeout
        if duration <= 0:
            raise ValueError("timeout must be positive")
        seq = self._next_seq
        self._next_seq += 1
        message: dict[str, Any] = {"seq": seq, "type": "request", "command": command}
        if arguments is not None:
            message["arguments"] = dict(arguments)
        deadline = time.monotonic() + duration
        self._pending[seq] = command
        try:
            self._write_all(frame(message), deadline)
            while seq not in self._responses:
                if self._stdout_eof:
                    raise DapError(f"DAP stdout closed before response to {command}")
                self._read(deadline)
            return self._responses.pop(seq)
        except (DapError, FrameError):
            self.abort()
            raise
        finally:
            self._pending.pop(seq, None)

    def initialize(self, *, adapter_id: str = "elisa", client_name: str = "Elisa IDE", timeout: float | None = None) -> dict[str, Any]:
        response = self.request(
            "initialize",
            {
                "adapterID": adapter_id,
                "clientID": "elisa-ide",
                "clientName": client_name,
                "linesStartAt1": True,
                "columnsStartAt1": False,
                "pathFormat": "path",
                "supportsVariableType": True,
                "supportsVariablePaging": False,
            },
            timeout=timeout,
        )
        if response.get("success") is True and isinstance(response.get("body"), dict):
            self.capabilities = response["body"]
        self.initialized = response.get("success") is True
        return response

    def cancel(self, request_id: int) -> dict[str, Any]:
        return self.request("cancel", {"requestId": request_id})

    def wait_event(self, event_name: str, *, timeout: float | None = None) -> dict[str, Any]:
        duration = self.request_timeout if timeout is None else timeout
        deadline = time.monotonic() + duration
        while True:
            for index, event in enumerate(self.events):
                if event.get("event") == event_name:
                    return self.events.pop(index)
            if self._stdout_eof:
                raise DapError(f"DAP stdout closed before event {event_name!r}")
            self._read(deadline)

    def supports(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability))

    def close(self, *, expect_exit: int = 0, timeout: float | None = None) -> int:
        if self._closed:
            return self.process.returncode if self.process and self.process.returncode is not None else 0
        process = self.process
        if process is None:
            self._closed = True
            return 0
        duration = self.teardown_timeout if timeout is None else timeout
        if process.poll() is None:
            try:
                if self.initialized:
                    self.request("disconnect", {"terminateDebuggee": True}, timeout=min(duration, self.request_timeout))
            except DapError:
                self.abort()
                raise
            try:
                process.wait(timeout=duration)
            except subprocess.TimeoutExpired:
                self.abort()
                raise DapError("DAP adapter did not exit before teardown deadline")
        status = process.returncode
        if status != expect_exit:
            details = self.stderr_text()[:2000]
            self.abort()
            raise DapError(f"DAP adapter exit code {status} != {expect_exit}; stderr={details!r}")
        self._closed = True
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return int(status)

    def abort(self) -> None:
        if self._closed:
            return
        process = self.process
        if process is not None:
            try:
                if process.poll() is None:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=0.3)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        self._closed = True
