"""Bounded LSP process supervision with lossless open-buffer replay.

The transport owns one subprocess session. This supervisor owns the workspace
view across sessions so an explicit restart can relaunch the server and reopen
the exact unsaved text at its current document version. Restarts are caller
driven and rate limited; the class never enters an automatic retry loop.
"""

from __future__ import annotations

import os
import time
import copy
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from .lsp_client import DiagnosticStore, LspClient, LspError, ServerCapabilities


class SupervisorState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


@dataclass(frozen=True)
class OpenDocument:
    uri: str
    text: str
    version: int
    language_id: str


@dataclass(frozen=True)
class LspFailure:
    message: str
    exit_code: int | None
    stderr: str
    occurred_at: float
    generation: int


class LspSupervisor:
    """Manage a bounded sequence of LSP sessions for one workspace."""

    def __init__(
        self,
        command: Iterable[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        initialize_params: Mapping[str, Any] | None = None,
        max_restarts: int = 3,
        restart_window_seconds: float = 60.0,
        client_options: Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(command, (str, bytes)):
            raise ValueError("LSP command must be a sequence of arguments")
        self.command = tuple(command)
        if not self.command or any(not isinstance(part, str) or not part for part in self.command):
            raise ValueError("LSP command must not be empty")
        if type(max_restarts) is not int or max_restarts < 0:
            raise ValueError("max_restarts must be a nonnegative integer")
        if not math.isfinite(restart_window_seconds) or restart_window_seconds <= 0:
            raise ValueError("restart_window_seconds must be finite and positive")
        self.cwd = os.fspath(cwd) if cwd is not None else None
        self.env = dict(env) if env is not None else None
        self.initialize_params = copy.deepcopy(dict(initialize_params)) if initialize_params is not None else None
        self.max_restarts = max_restarts
        self.restart_window_seconds = restart_window_seconds
        self.client_options = dict(client_options or {})
        reserved_options = {"command", "cwd", "env"}.intersection(self.client_options)
        if reserved_options:
            raise ValueError(f"client_options cannot override {', '.join(sorted(reserved_options))}")
        self.state = SupervisorState.STOPPED
        self.generation = 0
        self.last_failure: LspFailure | None = None
        self._ever_started = False
        self._client: LspClient | None = None
        self._documents: dict[str, OpenDocument] = {}
        self._diagnostics = DiagnosticStore()
        self._restart_times: list[float] = []

    @property
    def documents(self) -> Mapping[str, OpenDocument]:
        """Read-only snapshot of the unsaved workspace buffers."""
        return dict(self._documents)

    @property
    def restart_count(self) -> int:
        self._prune_restart_times(time.monotonic())
        return len(self._restart_times)

    @property
    def capabilities(self) -> ServerCapabilities:
        if self._client is None or self.state != SupervisorState.RUNNING:
            return ServerCapabilities({}, {})
        return self._client.server_capabilities

    @property
    def diagnostics(self) -> DiagnosticStore:
        return self._diagnostics

    def start(self) -> dict[str, Any]:
        """Start the first session, or perform a bounded restart thereafter."""
        if self.state == SupervisorState.RUNNING:
            raise LspError("LSP supervisor is already running")
        if self._ever_started:
            return self.restart()
        self._ever_started = True
        return self._launch()

    def _new_client(self) -> LspClient:
        return LspClient(
            self.command,
            cwd=self.cwd,
            env=self.env,
            diagnostic_store=self._diagnostics,
            **self.client_options,
        )

    def _launch(self) -> dict[str, Any]:
        client = self._new_client()
        self._client = client
        try:
            client.start()
            response = client.initialize(copy.deepcopy(self.initialize_params))
            if "error" in response:
                raise LspError(f"LSP initialize failed: {response['error']!r}")
            # Replay the exact unsaved buffer contents and current versions.
            # Never reload from disk as part of process recovery.
            for document in self._documents.values():
                client.open_document(
                    document.uri,
                    document.text,
                    version=document.version,
                    language_id=document.language_id,
                )
        except Exception as exc:
            self._record_failure(exc, client)
            if isinstance(exc, LspError):
                raise
            raise LspError(f"LSP server launch failed: {exc}") from exc
        self.generation += 1
        self.state = SupervisorState.RUNNING
        return response

    def _prune_restart_times(self, now: float) -> None:
        threshold = now - self.restart_window_seconds
        while self._restart_times and self._restart_times[0] <= threshold:
            self._restart_times.pop(0)

    def _record_failure(self, error: Exception, client: LspClient) -> None:
        process = client.process
        exit_code = process.poll() if process is not None else None
        client.abort()
        if process is not None:
            exit_code = process.returncode
        self.last_failure = LspFailure(
            message=str(error),
            exit_code=exit_code,
            stderr=client.stderr_text(),
            occurred_at=time.monotonic(),
            generation=self.generation,
        )
        self.state = SupervisorState.FAILED

    def check_health(self) -> bool:
        """Notice an asynchronous server exit and retain its failure details."""
        if self.state != SupervisorState.RUNNING or self._client is None:
            return False
        process = self._client.process
        if process is None:
            self._record_failure(LspError("LSP process handle is missing"), self._client)
            return False
        exit_code = process.poll()
        if exit_code is None:
            return True
        self._record_failure(
            LspError(f"LSP server exited with status {exit_code}"), self._client
        )
        return False

    def _require_running(self) -> LspClient:
        if self.state != SupervisorState.RUNNING or self._client is None:
            detail = self.last_failure.message if self.last_failure is not None else self.state.value
            raise LspError(f"LSP supervisor is not running: {detail}")
        if not self.check_health():
            detail = self.last_failure.message if self.last_failure is not None else "server exited"
            raise LspError(f"LSP supervisor detected a server failure: {detail}")
        assert self._client is not None
        return self._client

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        client = self._require_running()
        try:
            return getattr(client, method)(*args, **kwargs)
        except LspError as exc:
            process = client.process
            # Unsupported capabilities and locally rejected messages are not
            # process failures. The transport marks itself closed when a
            # timeout, framing error, or broken pipe requires aborting it.
            if client._closed or process is not None and process.poll() is not None:
                self._record_failure(exc, client)
            raise

    def open_document(
        self,
        uri: str,
        text: str,
        *,
        version: int = 1,
        language_id: str = "elisa",
    ) -> None:
        if uri in self._documents:
            raise LspError(f"document is already open: {uri}")
        self._call("open_document", uri, text, version=version, language_id=language_id)
        self._documents[uri] = OpenDocument(uri, text, version, language_id)

    def change_document(self, uri: str, text: str, *, version: int | None = None) -> int:
        document = self._documents.get(uri)
        if document is None:
            raise LspError(f"document is not open: {uri}")
        next_version = self._call("change_document", uri, text, version=version)
        self._documents[uri] = OpenDocument(
            uri, text, next_version, document.language_id
        )
        return next_version

    def save_document(self, uri: str) -> None:
        self._call("save_document", uri)

    def close_document(self, uri: str) -> None:
        if uri not in self._documents:
            return
        self._call("close_document", uri)
        self._documents.pop(uri, None)

    def request(self, method: str, params: Any | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("request", method, params, timeout=timeout)

    def wait_notification(self, method: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("wait_notification", method, timeout=timeout)

    def restart(self) -> dict[str, Any]:
        """Restart once, subject to a rolling limit, and replay open buffers."""
        if not self._ever_started:
            raise LspError("LSP supervisor has not been started")
        now = time.monotonic()
        self._prune_restart_times(now)
        if len(self._restart_times) >= self.max_restarts:
            failure = self.last_failure
            detail = f"; last exit={failure.exit_code}, stderr={failure.stderr[:512]!r}" if failure else ""
            raise LspError(
                f"LSP restart limit reached ({self.max_restarts} per "
                f"{self.restart_window_seconds:g}s){detail}"
            )
        self._restart_times.append(now)

        previous = self._client
        if previous is not None:
            if self.state == SupervisorState.RUNNING:
                try:
                    response = previous.shutdown()
                    if "error" in response:
                        raise LspError(f"LSP shutdown failed: {response['error']!r}")
                    previous.close()
                except LspError as exc:
                    self._record_failure(exc, previous)
            else:
                previous.abort()
        self._client = None
        self.state = SupervisorState.STOPPED
        return self._launch()

    def close(self) -> None:
        """Stop cleanly while retaining the in-memory document snapshot."""
        client = self._client
        if client is None:
            self.state = SupervisorState.STOPPED
            return
        if self.state == SupervisorState.RUNNING:
            try:
                response = client.shutdown()
                if "error" in response:
                    raise LspError(f"LSP shutdown failed: {response['error']!r}")
                client.close()
            except LspError as exc:
                self._record_failure(exc, client)
                self._client = None
                raise
        else:
            client.abort()
        self._client = None
        self.state = SupervisorState.STOPPED

    def abort(self) -> None:
        """Force-stop the current process without discarding open buffers."""
        client = self._client
        if client is not None:
            if self.state == SupervisorState.RUNNING and client.process is not None and client.process.poll() is None:
                client.abort()
                self.state = SupervisorState.STOPPED
            elif self.state == SupervisorState.RUNNING:
                self._record_failure(LspError("LSP server exited before abort"), client)
            else:
                client.abort()
        self._client = None

    def __enter__(self) -> "LspSupervisor":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self.abort()
        else:
            self.close()
        return False
