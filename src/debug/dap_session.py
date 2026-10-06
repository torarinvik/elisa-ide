"""Identity-checked, capability-gated managed Elisa debug sessions."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from debug.dap_client import DapClient, DapError, DapSessionIdentity
from debug.source_positions import edir_to_dap_position
from source import PositionError, SourceDocument
from toolchain.resolver import ToolRecord, resolve_tool


# These bounds are pinned to the adjacent Elisa Debugger EDIR/DAP contract:
# schema 4 accepts at most 37,729 artifact bytes, source paths are 1,024 UTF-8
# bytes, and one source replacement carries at most 256 breakpoints.
MAX_EDIR_BYTES = 37_729
MAX_DEBUG_SOURCE_PATH_BYTES = 1024
MAX_BREAKPOINTS_PER_SOURCE = 256
MAX_BREAKPOINT_SOURCE_BYTES = 1 << 20


class DebugSessionError(RuntimeError):
    """The requested debug action is invalid, stale, or unsupported."""


class DebugSessionState(Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    CONFIGURING = "configuring"
    RUNNING = "running"
    STOPPED = "stopped"
    EXITED = "exited"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True)
class DebugArtifact:
    path: Path
    sha256: str
    size: int

    @property
    def identity(self) -> str:
        return f"sha256:{self.sha256}"


@dataclass(frozen=True)
class BreakpointResult:
    requested_line: int
    verified: bool
    line: int | None
    message: str | None = None
    requested_byte_column: int | None = None
    dap_column: int | None = None


def inspect_debug_artifact(
    path: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_EDIR_BYTES,
) -> DebugArtifact:
    """Hash a bounded, regular EDIR artifact before the adapter is launched."""

    try:
        artifact_path = Path(path).expanduser().resolve(strict=True)
        if not artifact_path.is_file():
            raise DebugSessionError(f"debug artifact is not a regular file: {artifact_path}")
        size = artifact_path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise DebugSessionError(
                f"debug artifact size {size} is outside the supported range 1..{max_bytes} bytes"
            )
        digest = hashlib.sha256()
        observed = 0
        with artifact_path.open("rb") as source:
            while True:
                chunk = source.read(min(1024 * 1024, max_bytes + 1 - observed))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > max_bytes:
                    raise DebugSessionError(f"debug artifact exceeds {max_bytes} bytes")
                digest.update(chunk)
        if observed != size:
            raise DebugSessionError("debug artifact changed while it was being inspected")
    except DebugSessionError:
        raise
    except OSError as exc:
        raise DebugSessionError(f"could not inspect debug artifact {path!s}: {exc}") from exc

    actual = digest.hexdigest()
    if expected_sha256 is not None and actual != expected_sha256.lower():
        raise DebugSessionError("debug artifact hash no longer matches the successful build")
    return DebugArtifact(artifact_path, actual, observed)


def resolve_debug_adapter(
    workspace_root: str | os.PathLike[str],
    *,
    explicit: str | os.PathLike[str] | None = None,
    trusted_nearby: Iterable[str | os.PathLike[str]] = (),
    env: Mapping[str, str] | None = None,
) -> ToolRecord:
    """Resolve the adapter by explicit choice, trusted build, environment, then PATH."""

    record = resolve_tool(
        "elisa-debugger-dap-server",
        workspace_root,
        explicit=explicit,
        trusted_nearby=trusted_nearby,
        env_var="ELISA_DEBUGGER_DAP",
        env=env,
        path_name="elisa-debugger-dap-server",
    )
    assert record is not None
    return record


class ElisaDebugSession:
    """A single managed EDIR launch with immutable build identity.

    The session never compiles source. The caller must supply an EDIR artifact
    produced by the successful build revision it wants to debug. Adapter
    validation remains authoritative for the EDIR format and supported opcodes.
    """

    def __init__(
        self,
        command: Sequence[str],
        artifact_path: str | os.PathLike[str],
        source_revision: int,
        *,
        expected_artifact_sha256: str | None = None,
        edir_identity: str | None = None,
        source_path_root: str | os.PathLike[str] | None = None,
        adapter_cwd: str | os.PathLike[str] | None = None,
        adapter_env: Mapping[str, str] | None = None,
        request_timeout: float = 5.0,
    ) -> None:
        if type(source_revision) is not int or source_revision < 0:
            raise ValueError("source revision must be nonnegative")
        self.artifact = inspect_debug_artifact(
            artifact_path, expected_sha256=expected_artifact_sha256
        )
        self.source_revision = source_revision
        self.source_path_root = self._source_root(source_path_root)
        identity_text = edir_identity or self.artifact.identity
        if not identity_text or "\x00" in identity_text or len(identity_text.encode("utf-8")) > 256:
            raise ValueError("EDIR identity must be nonempty, NUL-free, and at most 256 UTF-8 bytes")
        self.identity = DapSessionIdentity(
            os.fspath(self.artifact.path),
            self.artifact.sha256,
            source_revision,
            identity_text,
        )
        self.client = DapClient(
            command,
            self.identity,
            cwd=adapter_cwd,
            env=adapter_env,
            request_timeout=request_timeout,
        )
        self.state = DebugSessionState.CREATED
        self.capabilities: Mapping[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self._pending_events: list[dict[str, Any]] = []
        self.breakpoints: dict[str, tuple[BreakpointResult, ...]] = {}
        self.failure: str | None = None

    @staticmethod
    def _source_root(value: str | os.PathLike[str] | None) -> Path | None:
        if value is None:
            return None
        try:
            root = Path(value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise DebugSessionError(f"debug source root does not exist: {value!s}: {exc}") from exc
        if not root.is_dir():
            raise DebugSessionError(f"debug source root is not a directory: {root}")
        if len(os.fspath(root).encode("utf-8")) > MAX_DEBUG_SOURCE_PATH_BYTES:
            raise DebugSessionError("debug source root exceeds the supported path limit")
        return root

    def _assert_artifact_unchanged(self) -> None:
        current = inspect_debug_artifact(self.artifact.path, expected_sha256=self.artifact.sha256)
        if current.size != self.artifact.size:
            raise DebugSessionError("debug artifact changed after session identity was created")

    def _success(self, response: Mapping[str, Any], command: str) -> Mapping[str, Any]:
        if response.get("success") is not True:
            message = response.get("message")
            detail = message if isinstance(message, str) and message else "adapter rejected the request"
            raise DebugSessionError(f"DAP {command} failed: {detail}")
        body = response.get("body", {})
        if not isinstance(body, dict):
            raise DebugSessionError(f"DAP {command} returned a non-object body")
        return body

    def _consume_event(self, event: Mapping[str, Any], *, queue: bool = True) -> None:
        recorded = dict(event)
        self.events.append(recorded)
        if queue:
            self._pending_events.append(recorded)
        name = event.get("event")
        if name == "stopped":
            self.state = DebugSessionState.STOPPED
        elif name == "continued":
            self.state = DebugSessionState.RUNNING
        elif name in ("terminated", "exited"):
            self.state = DebugSessionState.EXITED
        elif name == "initialized" and self.state == DebugSessionState.INITIALIZING:
            self.state = DebugSessionState.CONFIGURING

    def _collect_events(self) -> None:
        while self.client.events:
            event = self.client.events.pop(0)
            self._consume_event(event)

    def _request(self, command: str, arguments: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        try:
            body = self._success(self.client.request(command, arguments), command)
            self._collect_events()
            return body
        except (DapError, DebugSessionError) as exc:
            self.failure = str(exc)
            self.state = DebugSessionState.FAILED
            raise

    def start(
        self,
        breakpoints: Mapping[str | os.PathLike[str], Sequence[int]] | None = None,
    ) -> None:
        if self.state != DebugSessionState.CREATED:
            raise DebugSessionError("debug session can only be started once")
        try:
            self._assert_artifact_unchanged()
            self.state = DebugSessionState.INITIALIZING
            self.client.start()
            initialize = self.client.initialize()
            self._success(initialize, "initialize")
            self.capabilities = self.client.capabilities
            if not self.client.initialized:
                raise DebugSessionError("debug adapter did not accept initialization")

            # Adapter startup can take time; bind the actual launch to the same
            # bytes that were approved when the session was constructed.
            self._assert_artifact_unchanged()
            launch_arguments: dict[str, Any] = {"program": os.fspath(self.artifact.path)}
            if self.source_path_root is not None:
                launch_arguments["sourcePathRoot"] = os.fspath(self.source_path_root)
            # Some DAP adapters defer the successful launch response until
            # configurationDone. Observe either the initialized event or an
            # immediate launch rejection before proceeding with configuration.
            launch_sequence = self.client.send_request("launch", launch_arguments)
            launch_kind, launch_message = self.client.wait_event_or_response(
                launch_sequence, "initialized"
            )
            launch_response: Mapping[str, Any] | None = None
            if launch_kind == "response":
                launch_response = self._success(launch_message, "launch")
                initialized_event = self.client.wait_event("initialized")
            else:
                initialized_event = launch_message
            self._consume_event(initialized_event, queue=False)
            self.state = DebugSessionState.CONFIGURING
            self._collect_events()

            for source_path, lines in (breakpoints or {}).items():
                self.set_breakpoints(source_path, lines)

            if self.client.supports("supportsConfigurationDoneRequest"):
                self._request("configurationDone")
            if launch_response is None:
                self._success(self.client.wait_response(launch_sequence), "launch")
            self._collect_events()
            if self.state == DebugSessionState.CONFIGURING:
                self.state = DebugSessionState.RUNNING
        except (DapError, DebugSessionError, OSError, ValueError) as exc:
            self.failure = str(exc)
            self.state = DebugSessionState.FAILED
            self.client.abort()
            if isinstance(exc, (DapError, DebugSessionError)):
                raise
            raise DebugSessionError(str(exc)) from exc

    def set_breakpoints(
        self,
        source_path: str | os.PathLike[str],
        lines: Sequence[int],
    ) -> tuple[BreakpointResult, ...]:
        if len(lines) > MAX_BREAKPOINTS_PER_SOURCE:
            raise DebugSessionError("too many breakpoints for one source file")
        if any(type(line) is not int or line < 1 or line > 100_000_000 for line in lines):
            raise DebugSessionError("breakpoint lines must be positive one-based source lines")
        requests = [{"line": line} for line in lines]
        requested = [(line, None) for line in lines]
        return self._replace_source_breakpoints(source_path, requests, requested)

    def set_edir_breakpoints(
        self,
        source_path: str | os.PathLike[str],
        source_text: str,
        positions: Sequence[tuple[int, int]],
    ) -> tuple[BreakpointResult, ...]:
        """Replace breakpoints from one-based EDIR line/UTF-8-byte coordinates."""

        if not isinstance(source_text, str):
            raise TypeError("breakpoint source snapshot must be text")
        try:
            source_bytes = source_text.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise DebugSessionError(f"breakpoint source snapshot is not valid UTF-8 text: {exc}") from exc
        if len(source_bytes) > MAX_BREAKPOINT_SOURCE_BYTES:
            raise DebugSessionError("breakpoint source snapshot exceeds the 1 MiB limit")
        if len(positions) > MAX_BREAKPOINTS_PER_SOURCE:
            raise DebugSessionError("too many breakpoints for one source file")
        try:
            document = SourceDocument(source_bytes, max_history_bytes=MAX_BREAKPOINT_SOURCE_BYTES)
            requests: list[dict[str, int]] = []
            requested: list[tuple[int, int | None]] = []
            for position in positions:
                if (not isinstance(position, tuple) or len(position) != 2
                        or type(position[0]) is not int or type(position[1]) is not int
                        or position[0] < 1 or position[1] < 1):
                    raise DebugSessionError("EDIR breakpoint positions must be positive (line, byte-column) pairs")
                dap_line, dap_column = edir_to_dap_position(document, position[0], position[1])
                requests.append({"line": dap_line, "column": dap_column})
                requested.append(position)
        except PositionError as exc:
            raise DebugSessionError(f"EDIR breakpoint position is outside the source snapshot: {exc}") from exc
        except UnicodeError as exc:
            raise DebugSessionError(f"breakpoint source snapshot is not valid UTF-8 text: {exc}") from exc
        return self._replace_source_breakpoints(source_path, requests, requested)

    def _replace_source_breakpoints(
        self,
        source_path: str | os.PathLike[str],
        requests: Sequence[Mapping[str, int]],
        requested: Sequence[tuple[int, int | None]],
    ) -> tuple[BreakpointResult, ...]:
        if self.state not in (DebugSessionState.CONFIGURING, DebugSessionState.STOPPED, DebugSessionState.RUNNING):
            raise DebugSessionError("breakpoints require an initialized debug session")
        if len(requests) != len(requested) or len(requests) > MAX_BREAKPOINTS_PER_SOURCE:
            raise DebugSessionError("breakpoint request count is invalid")
        path = Path(source_path).expanduser().resolve(strict=False)
        if path.suffix != ".elisa":
            raise DebugSessionError("breakpoint source must be an Elisa .elisa file")
        if self.source_path_root is not None:
            try:
                path.relative_to(self.source_path_root)
            except ValueError as exc:
                raise DebugSessionError("breakpoint source is outside the configured source root") from exc
        path_text = os.fspath(path)
        encoded = path_text.encode("utf-8")
        if not encoded or len(encoded) > MAX_DEBUG_SOURCE_PATH_BYTES or b"\x00" in encoded:
            raise DebugSessionError("breakpoint source path is invalid or exceeds the path limit")
        if not self.client.initialized:
            raise DebugSessionError("debug adapter is not initialized")
        body = self._request(
            "setBreakpoints",
            {"source": {"path": path_text}, "breakpoints": [dict(item) for item in requests]},
        )
        returned = body.get("breakpoints", [])
        if not isinstance(returned, list):
            raise DebugSessionError("DAP setBreakpoints returned an invalid breakpoints array")
        results: list[BreakpointResult] = []
        for index, (requested_line, requested_column) in enumerate(requested):
            record = returned[index] if index < len(returned) else {}
            if not isinstance(record, dict):
                record = {}
            actual_line = record.get("line")
            if type(actual_line) is not int or actual_line < 1:
                actual_line = None
            message = record.get("message")
            dap_column = record.get("column")
            if type(dap_column) is not int or dap_column < 0:
                dap_column = None
            results.append(BreakpointResult(
                requested_line,
                record.get("verified") is True,
                actual_line,
                message if isinstance(message, str) else None,
                requested_column,
                dap_column,
            ))
        output = tuple(results)
        self.breakpoints[path_text] = output
        return output

    def wait_event(self, name: str, *, timeout: float | None = None) -> Mapping[str, Any]:
        if not name:
            raise ValueError("event name must be nonempty")
        for index, event in enumerate(self._pending_events):
            if event.get("event") == name:
                return self._pending_events.pop(index)
        try:
            event = self.client.wait_event(name, timeout=timeout)
            self._consume_event(event, queue=False)
            return event
        except DapError as exc:
            self.failure = str(exc)
            self.state = DebugSessionState.FAILED
            raise

    def poll_events(self, *, timeout: float = 0.0) -> tuple[Mapping[str, Any], ...]:
        """Pump DAP output without turning an idle interval into a failure."""

        if self.state in (DebugSessionState.CREATED, DebugSessionState.CLOSED):
            raise DebugSessionError("debug events require an active session")
        try:
            self.client.poll_available(timeout)
            self._collect_events()
            pending = tuple(self._pending_events)
            self._pending_events.clear()
            return pending
        except DapError as exc:
            self.failure = str(exc)
            self.state = DebugSessionState.FAILED
            raise

    def threads(self) -> list[Mapping[str, Any]]:
        body = self._request("threads")
        values = body.get("threads", [])
        if not isinstance(values, list):
            raise DebugSessionError("DAP threads returned an invalid array")
        return [value for value in values if isinstance(value, dict)]

    def stack_trace(self, thread_id: int, *, start_frame: int = 0, levels: int = 64) -> list[Mapping[str, Any]]:
        if type(thread_id) is not int or thread_id <= 0 or start_frame < 0 or levels < 0:
            raise ValueError("thread, start frame, and stack level operands are invalid")
        body = self._request("stackTrace", {"threadId": thread_id, "startFrame": start_frame, "levels": levels})
        values = body.get("stackFrames", [])
        if not isinstance(values, list):
            raise DebugSessionError("DAP stackTrace returned an invalid frame array")
        return [value for value in values if isinstance(value, dict)]

    def scopes(self, frame_id: int) -> list[Mapping[str, Any]]:
        if type(frame_id) is not int or frame_id <= 0:
            raise ValueError("frame ID must be positive")
        body = self._request("scopes", {"frameId": frame_id})
        values = body.get("scopes", [])
        if not isinstance(values, list):
            raise DebugSessionError("DAP scopes returned an invalid array")
        return [value for value in values if isinstance(value, dict)]

    def variables(self, reference: int, *, start: int = 0, count: int = 0) -> list[Mapping[str, Any]]:
        if type(reference) is not int or reference <= 0 or start < 0 or count < 0:
            raise ValueError("variables reference or page operands are invalid")
        body = self._request("variables", {"variablesReference": reference, "start": start, "count": count})
        values = body.get("variables", [])
        if not isinstance(values, list):
            raise DebugSessionError("DAP variables returned an invalid array")
        return [value for value in values if isinstance(value, dict)]

    def evaluate(self, expression: str, *, frame_id: int | None = None) -> Mapping[str, Any]:
        if not self.client.supports("supportsEvaluateForHovers"):
            raise DebugSessionError("the active adapter does not support expression evaluation")
        if not expression or len(expression.encode("utf-8")) > 4096 or "\x00" in expression:
            raise DebugSessionError("debug expression is empty or exceeds the supported limit")
        arguments: dict[str, Any] = {"expression": expression, "context": "watch"}
        if frame_id is not None:
            if type(frame_id) is not int or frame_id <= 0:
                raise ValueError("frame ID must be positive")
            arguments["frameId"] = frame_id
        return self._request("evaluate", arguments)

    def continue_execution(self, thread_id: int) -> Mapping[str, Any]:
        if type(thread_id) is not int or thread_id <= 0:
            raise ValueError("thread ID must be positive")
        self.state = DebugSessionState.RUNNING
        return self._request("continue", {"threadId": thread_id})

    def step(self, command: str, thread_id: int) -> Mapping[str, Any]:
        if command not in ("next", "stepIn", "stepOut", "stepBack"):
            raise ValueError(f"unsupported step command {command!r}")
        if type(thread_id) is not int or thread_id <= 0:
            raise ValueError("thread ID must be positive")
        if command == "stepBack" and not self.client.supports("supportsStepBack"):
            raise DebugSessionError("the active adapter does not support reverse step")
        self.state = DebugSessionState.RUNNING
        return self._request(command, {"threadId": thread_id})

    def pause(self, thread_id: int) -> Mapping[str, Any]:
        if type(thread_id) is not int or thread_id <= 0:
            raise ValueError("thread ID must be positive")
        return self._request("pause", {"threadId": thread_id})

    def terminate(self) -> Mapping[str, Any]:
        if not self.client.supports("supportsTerminateRequest"):
            raise DebugSessionError("the active adapter does not support terminate")
        body = self._request("terminate")
        self._collect_events()
        return body

    def close(self) -> int:
        if self.state == DebugSessionState.CLOSED:
            return 0
        try:
            result = self.client.close()
            self._collect_events()
            self.state = DebugSessionState.CLOSED
            return result
        except DapError as exc:
            self.failure = str(exc)
            self.state = DebugSessionState.FAILED
            raise
