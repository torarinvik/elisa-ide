"""Bounded JSON-RPC/LSP transport used by Elisa IDE services.

The desktop shell is written in Elisa, while language servers are external
processes.  This module owns the byte-stream boundary so future source-buffer
and Problems integrations do not each invent framing, timeout, or version
rules.  It intentionally has no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping
from urllib.parse import quote

if TYPE_CHECKING:
    from source import SourceDocument


class LspError(RuntimeError):
    """A process, deadline, framing, or JSON-RPC contract failure."""


class FrameError(LspError):
    """A malformed, oversized, or truncated LSP frame."""


def _reject_constant(value: str) -> None:
    raise FrameError(f"non-finite JSON number {value!r}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _decode_json(body: bytes) -> dict[str, Any]:
    try:
        source = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FrameError(f"LSP body is not UTF-8: {exc}") from exc
    try:
        value = json.loads(
            source,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except FrameError:
        raise
    except (ValueError, RecursionError) as exc:
        raise FrameError(f"LSP body is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FrameError("LSP JSON body must be an object")
    return value


def _id_key(value: Any, *, response: bool = False) -> tuple[str, Any]:
    if type(value) is int:
        if not -(2**31) <= value <= 2**31 - 1:
            raise FrameError(f"JSON-RPC integer ID outside signed 32-bit range: {value}")
        return ("int", value)
    if isinstance(value, str):
        return ("str", value)
    if response and value is None:
        return ("null", None)
    suffix = " (or null for an error response)" if response else ""
    raise FrameError(f"JSON-RPC ID must be a string or signed 32-bit integer{suffix}")


def frame(message: Any) -> bytes:
    """Encode one JSON-RPC object and count its UTF-8 bytes exactly."""
    if isinstance(message, (bytes, bytearray, memoryview)):
        body = bytes(message)
    else:
        try:
            body = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise LspError(f"cannot encode LSP JSON: {exc}") from exc
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


_HEADER_NAME = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_CONTENT_LENGTH = re.compile(rb"[0-9]+\Z")


@dataclass(frozen=True)
class LspFrame:
    message: dict[str, Any]
    body: bytes


class FrameDecoder:
    """Incremental strict decoder with independent header/body limits."""

    def __init__(self, *, max_header_bytes: int = 8192, max_body_bytes: int = 16 << 20):
        if max_header_bytes < 1 or max_body_bytes < 1:
            raise ValueError("frame limits must be positive")
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.buffer = bytearray()
        self.expected_body: int | None = None

    def _length(self, header: bytes) -> int:
        if len(header) > self.max_header_bytes:
            raise FrameError(f"LSP header exceeds {self.max_header_bytes} bytes")
        fields: dict[bytes, bytes] = {}
        lines = header.split(b"\r\n")
        if not lines or any(not line for line in lines):
            raise FrameError("LSP header contains an empty line")
        for line in lines:
            name, colon, value = line.partition(b":")
            if not colon or not _HEADER_NAME.fullmatch(name):
                raise FrameError(f"malformed LSP header line: {line!r}")
            folded = name.lower()
            if folded in fields:
                raise FrameError(f"duplicate LSP header field {name!r}")
            value = value.strip(b" \t")
            if any(byte < 0x20 and byte != 0x09 or byte == 0x7F for byte in value):
                raise FrameError(f"control byte in LSP header field {name!r}")
            fields[folded] = value
        raw_length = fields.get(b"content-length")
        if raw_length is None or not _CONTENT_LENGTH.fullmatch(raw_length):
            raise FrameError("LSP header needs a decimal Content-Length")
        length = int(raw_length)
        if length > self.max_body_bytes:
            raise FrameError(
                f"LSP body length {length} exceeds {self.max_body_bytes} bytes"
            )
        return length

    def feed(self, data: bytes) -> list[LspFrame]:
        if data:
            self.buffer.extend(data)
        result: list[LspFrame] = []
        marker = b"\r\n\r\n"
        while True:
            if self.expected_body is None:
                end = self.buffer.find(marker)
                if end < 0:
                    if len(self.buffer) > self.max_header_bytes:
                        raise FrameError(
                            f"LSP header exceeds {self.max_header_bytes} bytes"
                        )
                    break
                self.expected_body = self._length(bytes(self.buffer[:end]))
                del self.buffer[: end + len(marker)]
            assert self.expected_body is not None
            if len(self.buffer) < self.expected_body:
                break
            length = self.expected_body
            body = bytes(self.buffer[:length])
            del self.buffer[:length]
            self.expected_body = None
            result.append(LspFrame(_decode_json(body), body))
        return result

    def finish(self) -> None:
        if self.expected_body is not None:
            raise FrameError(
                f"truncated LSP body: expected {self.expected_body}, received {len(self.buffer)}"
            )
        if self.buffer:
            raise FrameError(f"truncated LSP header: {bytes(self.buffer[:160])!r}")


def path_to_uri(path: str | os.PathLike[str]) -> str:
    """Return a canonical file URI without requiring the path to exist."""
    value = os.path.abspath(os.fspath(path))
    # quote keeps slash separators while escaping spaces, accents, and emoji.
    return "file://" + quote(value, safe="/:")


@dataclass(frozen=True)
class ServerCapabilities:
    """The initialize response reduced to the facts the IDE can gate on."""

    server_info: Mapping[str, Any]
    capabilities: Mapping[str, Any]
    position_encoding: str | None = None
    document_sync_kind: int | None = None
    document_open_close: bool | None = None
    document_save: bool | None = None
    semantic_token_types: tuple[str, ...] = ()
    semantic_token_modifiers: tuple[str, ...] = ()
    workspace_folders_supported: bool = False
    workspace_folder_change_notifications: bool = False

    @classmethod
    def from_initialize(cls, result: Mapping[str, Any]) -> "ServerCapabilities":
        server_info = result.get("serverInfo", {})
        if not isinstance(server_info, Mapping):
            server_info = {}
        capabilities = result.get("capabilities", {})
        if not isinstance(capabilities, Mapping):
            raise FrameError("LSP initialize capabilities must be an object")
        position = capabilities.get("positionEncoding")
        if position is not None:
            if position not in {"utf-8", "utf-16", "utf-32"}:
                raise FrameError(f"unsupported LSP position encoding {position!r}")
        sync = capabilities.get("textDocumentSync")
        sync_kind: int | None = None
        open_close: bool | None = None
        save: bool | None = None
        if type(sync) is int:
            if sync not in {0, 1, 2}:
                raise FrameError("LSP textDocumentSync kind must be 0, 1, or 2")
            sync_kind = sync
        elif isinstance(sync, Mapping):
            raw_change = sync.get("change")
            if raw_change is not None:
                if type(raw_change) is not int or raw_change not in {0, 1, 2}:
                    raise FrameError("LSP textDocumentSync.change must be 0, 1, or 2")
                sync_kind = raw_change
            if "openClose" in sync:
                if type(sync["openClose"]) is not bool:
                    raise FrameError("LSP textDocumentSync.openClose must be boolean")
                open_close = sync["openClose"]
            if "save" in sync:
                save = sync["save"] is not False
                if type(sync["save"]) not in {bool, dict}:
                    raise FrameError("LSP textDocumentSync.save has an invalid shape")
        elif sync is not None:
            raise FrameError("LSP textDocumentSync has an invalid shape")
        semantic_types: tuple[str, ...] = ()
        semantic_modifiers: tuple[str, ...] = ()
        semantic = capabilities.get("semanticTokensProvider")
        if isinstance(semantic, Mapping):
            legend = semantic.get("legend")
            if legend is not None:
                if not isinstance(legend, Mapping):
                    raise FrameError("semanticTokensProvider.legend must be an object")
                raw_types = legend.get("tokenTypes", [])
                raw_modifiers = legend.get("tokenModifiers", [])
                if not isinstance(raw_types, list) or not all(isinstance(item, str) for item in raw_types):
                    raise FrameError("semantic token types must be a string array")
                if not isinstance(raw_modifiers, list) or not all(isinstance(item, str) for item in raw_modifiers):
                    raise FrameError("semantic token modifiers must be a string array")
                semantic_types = tuple(raw_types)
                semantic_modifiers = tuple(raw_modifiers)
        workspace_folders_supported = False
        workspace_folder_change_notifications = False
        workspace = capabilities.get("workspace")
        if workspace is not None and not isinstance(workspace, Mapping):
            raise FrameError("LSP workspace capabilities must be an object")
        if isinstance(workspace, Mapping):
            folders = workspace.get("workspaceFolders")
            if folders is not None and not isinstance(folders, Mapping):
                raise FrameError("LSP workspace.workspaceFolders must be an object")
            if isinstance(folders, Mapping):
                supported = folders.get("supported")
                if supported is not None and type(supported) is not bool:
                    raise FrameError("LSP workspaceFolders.supported must be boolean")
                change_notifications = folders.get("changeNotifications")
                if change_notifications is not None and not (
                    type(change_notifications) is bool or isinstance(change_notifications, str)
                ):
                    raise FrameError("LSP workspaceFolders.changeNotifications has an invalid shape")
                workspace_folders_supported = supported is True
                workspace_folder_change_notifications = bool(change_notifications)
        return cls(
            server_info=dict(server_info),
            capabilities=dict(capabilities),
            position_encoding=position,
            document_sync_kind=sync_kind,
            document_open_close=open_close,
            document_save=save,
            semantic_token_types=semantic_types,
            semantic_token_modifiers=semantic_modifiers,
            workspace_folders_supported=workspace_folders_supported,
            workspace_folder_change_notifications=workspace_folder_change_notifications,
        )

    def supports(self, key: str) -> bool:
        value = self.capabilities.get(key)
        return bool(value) if isinstance(value, (bool, dict, list, str)) else value is not None


@dataclass(frozen=True)
class PublishedDiagnostic:
    """Stable, source-facing projection of one LSP diagnostic."""

    uri: str
    version: int
    message: str
    severity: int | None
    source: str | None
    code: str | int | None
    start_line: int | None
    start_character: int | None
    end_line: int | None
    end_character: int | None
    related_information: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    def byte_range(
        self,
        document: SourceDocument,
        *,
        encoding: str,
    ) -> tuple[int, int] | None:
        """Map the negotiated LSP range to UTF-8 byte offsets.

        LSP positions are line/character pairs whose character unit comes
        from initialize negotiation. Source buffers store UTF-8 bytes, so
        callers should use this boundary rather than doing ad hoc arithmetic.
        Malformed, out-of-document, split-code-point, or reversed ranges are
        unavailable for navigation while the original diagnostic remains
        intact in ``raw``.
        """

        if encoding not in {"utf-8", "utf-16", "utf-32"}:
            raise ValueError(f"unsupported LSP position encoding {encoding!r}")
        if (self.start_line is None or self.start_character is None
                or self.end_line is None or self.end_character is None):
            return None
        try:
            start = document.position_to_byte(
                self.start_line, self.start_character, encoding=encoding
            )
            end = document.position_to_byte(
                self.end_line, self.end_character, encoding=encoding
            )
        except ValueError:
            return None
        if end < start:
            return None
        return start, end

    @classmethod
    def from_mapping(cls, uri: str, version: int, item: Mapping[str, Any]) -> "PublishedDiagnostic | None":
        message = item.get("message")
        if not isinstance(message, str):
            return None
        severity = item.get("severity")
        if type(severity) is not int:
            severity = None
        elif severity < 1 or severity > 4:
            severity = None
        source = item.get("source") if isinstance(item.get("source"), str) else None
        code_value = item.get("code")
        code: str | int | None = code_value if isinstance(code_value, (str, int)) and not isinstance(code_value, bool) else None
        start_line: int | None = None
        start_character: int | None = None
        end_line: int | None = None
        end_character: int | None = None
        location = item.get("range")
        if isinstance(location, Mapping):
            start = location.get("start")
            end = location.get("end")
            if isinstance(start, Mapping) and type(start.get("line")) is int and type(start.get("character")) is int:
                if start["line"] >= 0 and start["character"] >= 0:
                    start_line = start["line"]
                    start_character = start["character"]
            if isinstance(end, Mapping) and type(end.get("line")) is int and type(end.get("character")) is int:
                if end["line"] >= 0 and end["character"] >= 0:
                    end_line = end["line"]
                    end_character = end["character"]
        related_raw = item.get("relatedInformation")
        related: list[dict[str, Any]] = []
        if isinstance(related_raw, list):
            related.extend(dict(value) for value in related_raw if isinstance(value, Mapping))
        return cls(
            uri=uri,
            version=version,
            message=message,
            severity=severity,
            source=source,
            code=code,
            start_line=start_line,
            start_character=start_character,
            end_line=end_line,
            end_character=end_character,
            related_information=tuple(related),
            raw=dict(item),
        )


class DiagnosticStore:
    """Version-gated diagnostics shared by source and Problems projections."""

    def __init__(self) -> None:
        self._versions: dict[str, int] = {}
        self._items: dict[str, list[dict[str, Any]]] = {}
        self._typed: dict[str, tuple[PublishedDiagnostic, ...]] = {}

    def set_document_version(self, uri: str, version: int) -> None:
        if type(version) is not int or version < 0:
            raise ValueError("document versions must be nonnegative")
        previous = self._versions.get(uri)
        if previous is not None and previous != version:
            # Keep Problems aligned with the active buffer. A result from the
            # previous text must not remain presented as current while the
            # server computes diagnostics for the new version.
            self._items.pop(uri, None)
            self._typed.pop(uri, None)
        self._versions[uri] = version

    def publish(self, uri: str, version: int | None, diagnostics: Iterable[Mapping[str, Any]]) -> bool:
        current = self._versions.get(uri)
        if current is None or type(version) is not int or version != current:
            return False
        copied: list[dict[str, Any]] = []
        typed: list[PublishedDiagnostic] = []
        for item in diagnostics:
            if isinstance(item, Mapping):
                raw = dict(item)
                copied.append(raw)
                normalized = PublishedDiagnostic.from_mapping(uri, version, raw)
                if normalized is not None:
                    typed.append(normalized)
        self._items[uri] = copied
        self._typed[uri] = tuple(typed)
        return True

    def diagnostics(self, uri: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items.get(uri, [])]

    def typed(self, uri: str) -> tuple[PublishedDiagnostic, ...]:
        """Return normalized diagnostics for Problems/source projections."""
        return tuple(self._typed.get(uri, ()))

    def version(self, uri: str) -> int | None:
        return self._versions.get(uri)

    def remove(self, uri: str) -> None:
        self._versions.pop(uri, None)
        self._items.pop(uri, None)
        self._typed.pop(uri, None)


class LspClient:
    """A bounded, cancellable stdio LSP subprocess session."""

    def __init__(
        self,
        command: Iterable[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        startup_timeout: float = 5.0,
        request_timeout: float = 5.0,
        teardown_timeout: float = 3.0,
        max_header_bytes: int = 8192,
        max_body_bytes: int = 16 << 20,
        max_stream_bytes: int = 64 << 20,
        max_messages: int = 100_000,
        stderr_limit: int = 64 << 10,
        diagnostic_store: DiagnosticStore | None = None,
    ) -> None:
        self.command = list(command)
        if not self.command:
            raise ValueError("LSP command must not be empty")
        if startup_timeout <= 0 or request_timeout <= 0 or teardown_timeout <= 0:
            raise ValueError("LSP timeouts must be positive")
        if max_stream_bytes < 1 or max_messages < 1 or stderr_limit < 0:
            raise ValueError("LSP limits are invalid")
        self.cwd = os.fspath(cwd) if cwd is not None else None
        self.env = dict(env) if env is not None else None
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.teardown_timeout = teardown_timeout
        self.max_stream_bytes = max_stream_bytes
        self.max_messages = max_messages
        self.stderr_limit = stderr_limit
        self.decoder = FrameDecoder(
            max_header_bytes=max_header_bytes,
            max_body_bytes=max_body_bytes,
        )
        self.process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._responses: dict[tuple[str, Any], dict[str, Any]] = {}
        self._pending: set[tuple[str, Any]] = set()
        self._notifications: list[dict[str, Any]] = []
        self._server_requests: list[dict[str, Any]] = []
        self._registrations: dict[str, str] = {}
        self._stream_bytes = 0
        self._message_count = 0
        self._stdout_eof = False
        self._closed = False
        self._stderr = bytearray()
        self._stderr_truncated = False
        self._stderr_thread: threading.Thread | None = None
        self.documents: dict[str, tuple[str, int]] = {}
        self.diagnostics = diagnostic_store if diagnostic_store is not None else DiagnosticStore()
        self.server_info: Mapping[str, Any] = {}
        self.capabilities: Mapping[str, Any] = {}
        self.server_capabilities = ServerCapabilities({}, {})
        self.workspace_root_uri: str | None = None
        self.workspace_folder_uris: tuple[str, ...] = ()
        self.workspace_folder_names: dict[str, str] = {}
        self.configuration: Mapping[str, Any] = {}

    def __enter__(self) -> "LspClient":
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
            raise LspError("LSP client is already started")
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
            raise LspError(f"could not start LSP server {self.command!r}: {exc}") from exc
        assert self.process.stderr is not None
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        time.sleep(min(0.01, self.startup_timeout / 10))
        if self.process.poll() is not None:
            status = self.process.returncode
            details = self.stderr_text()[:2000]
            self.abort()
            raise LspError(f"LSP server exited during startup ({status}); stderr={details!r}")

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
            raise LspError("LSP client is not started")
        if self._closed:
            raise LspError("LSP client is closed")
        status = self.process.poll()
        if status is not None:
            raise LspError(f"LSP server exited with status {status}; stderr={self.stderr_text()[:2000]!r}")
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
                raise LspError("timed out writing to LSP server; subprocess killed and reaped")
            try:
                _, writable, _ = select.select([], [fd], [], remaining)
            except InterruptedError:
                continue
            if not writable:
                self.abort()
                raise LspError("timed out waiting for writable LSP stdin; subprocess killed and reaped")
            try:
                count = os.write(fd, payload[offset:])
            except (BrokenPipeError, OSError) as exc:
                self.abort()
                raise LspError(f"LSP stdin closed: {exc}") from exc
            if count <= 0:
                self.abort()
                raise LspError("LSP stdin accepted no bytes")
            offset += count

    def send(self, message: Any, *, timeout: float | None = None) -> None:
        duration = self.request_timeout if timeout is None else timeout
        if duration <= 0:
            raise ValueError("timeout must be positive")
        payload = frame(message)
        header_end = payload.find(b"\r\n\r\n")
        if header_end < 0 or header_end > self.decoder.max_header_bytes:
            raise FrameError("outgoing LSP header exceeds the configured message limit")
        body_size = len(payload) - header_end - 4
        if body_size > self.decoder.max_body_bytes:
            raise FrameError(
                f"outgoing LSP body exceeds {self.decoder.max_body_bytes} bytes"
            )
        self._write_all(payload, time.monotonic() + duration)

    def notify(self, method: str, params: Any | None = None, *, timeout: float | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message, timeout=timeout)

    def _read_frames(self, deadline: float) -> list[LspFrame]:
        process = self._ensure_running()
        assert process.stdout is not None
        fd = process.stdout.fileno()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LspError("timed out waiting for LSP output")
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except InterruptedError:
            return []
        if not readable:
            raise LspError("timed out waiting for LSP output")
        chunk = os.read(fd, 8192)
        if not chunk:
            self._stdout_eof = True
            self.decoder.finish()
            return []
        self._stream_bytes += len(chunk)
        if self._stream_bytes > self.max_stream_bytes:
            raise FrameError(f"LSP stdout exceeds {self.max_stream_bytes} bytes")
        frames = self.decoder.feed(chunk)
        for item in frames:
            self._record(item)
        return frames

    def _record(self, item: LspFrame) -> None:
        self._message_count += 1
        if self._message_count > self.max_messages:
            raise FrameError(f"LSP message count exceeds {self.max_messages}")
        message = item.message
        if message.get("jsonrpc") != "2.0":
            raise FrameError("LSP message must contain jsonrpc: '2.0'")
        has_id = "id" in message
        has_method = "method" in message
        has_result = "result" in message
        has_error = "error" in message
        if has_method:
            if not isinstance(message["method"], str) or has_result or has_error:
                raise FrameError("malformed LSP request or notification")
            if has_id:
                _id_key(message["id"])
                if not self._handle_server_request(message):
                    self._server_requests.append(message)
            else:
                self._notifications.append(message)
                if message["method"] == "textDocument/publishDiagnostics":
                    params = message.get("params")
                    if isinstance(params, dict) and isinstance(params.get("uri"), str):
                        version = params.get("version")
                        diagnostics = params.get("diagnostics", [])
                        if type(version) is int and isinstance(diagnostics, list):
                            self.diagnostics.publish(params["uri"], version, diagnostics)
            return
        if not has_id or has_result == has_error:
            raise FrameError("LSP response needs an ID and exactly one result/error")
        key = _id_key(message["id"], response=True)
        if key not in self._pending:
            raise FrameError(f"response arrived for unknown JSON-RPC ID {message['id']!r}")
        if has_error:
            error = message["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                raise FrameError("LSP error response has an invalid shape")
        if key in self._responses:
            raise FrameError(f"duplicate LSP response for ID {message['id']!r}")
        self._responses[key] = message

    def _server_response(self, request_id: int | str, *, result: Any = None,
                         error: Mapping[str, Any] | None = None) -> None:
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is None:
            response["result"] = result
        else:
            response["error"] = dict(error)
        self.send(response, timeout=self.request_timeout)

    @staticmethod
    def _configuration_section(settings: Mapping[str, Any], section: str | None) -> Any:
        if not section:
            return dict(settings)
        value: Any = settings
        for component in section.split("."):
            if not component or not isinstance(value, Mapping) or component not in value:
                return None
            value = value[component]
        return value

    def _handle_server_request(self, message: Mapping[str, Any]) -> bool:
        """Answer standard client registration/configuration requests locally."""
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, Mapping) else None
            if (not isinstance(items, list)
                    or any(not isinstance(item, Mapping) for item in items)
                    or any(item.get("section") is not None and not isinstance(item.get("section"), str)
                           for item in items if isinstance(item, Mapping))):
                self._server_response(request_id, error={"code": -32602, "message": "invalid workspace/configuration items"})
            else:
                settings = [self._configuration_section(self.configuration, item.get("section"))
                            for item in items]
                self._server_response(request_id, result=settings)
            return True
        if method == "client/registerCapability":
            registrations = params.get("registrations") if isinstance(params, Mapping) else None
            if not isinstance(registrations, list):
                self._server_response(request_id, error={"code": -32602, "message": "registrations must be an array"})
                return True
            parsed: list[tuple[str, str]] = []
            for registration in registrations:
                if not isinstance(registration, Mapping):
                    self._server_response(request_id, error={"code": -32602, "message": "registration must be an object"})
                    return True
                registration_id = registration.get("id")
                registered_method = registration.get("method")
                if not isinstance(registration_id, str) or not registration_id or not isinstance(registered_method, str):
                    self._server_response(request_id, error={"code": -32602, "message": "registration needs a nonempty id and method"})
                    return True
                parsed.append((registration_id, registered_method))
            self._registrations.update(parsed)
            self._server_response(request_id)
            return True
        if method == "client/unregisterCapability":
            removals = params.get("unregisterations") if isinstance(params, Mapping) else None
            if not isinstance(removals, list):
                self._server_response(request_id, error={"code": -32602, "message": "unregisterations must be an array"})
                return True
            parsed_ids: list[tuple[str, str]] = []
            for removal in removals:
                if not isinstance(removal, Mapping):
                    self._server_response(request_id, error={"code": -32602, "message": "unregistration must be an object"})
                    return True
                registration_id = removal.get("id")
                registered_method = removal.get("method")
                if not isinstance(registration_id, str) or not isinstance(registered_method, str):
                    self._server_response(request_id, error={"code": -32602, "message": "unregistration needs an id and method"})
                    return True
                parsed_ids.append((registration_id, registered_method))
            for registration_id, registered_method in parsed_ids:
                registered = self._registrations.get(registration_id)
                if registered is not None and registered != registered_method:
                    self._server_response(request_id, error={"code": -32602, "message": "unregistration method does not match its registration"})
                    return True
            for registration_id, _ in parsed_ids:
                self._registrations.pop(registration_id, None)
            self._server_response(request_id)
            return True
        return False

    def _wait_response(self, request_id: int | str, deadline: float) -> dict[str, Any]:
        key = _id_key(request_id)
        while key not in self._responses:
            if self._stdout_eof:
                raise LspError(f"LSP server closed stdout before response {request_id!r}")
            try:
                self._read_frames(deadline)
            except (LspError, FrameError):
                self.abort()
                raise
        return self._responses.pop(key)

    def request(self, method: str, params: Any | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        duration = self.request_timeout if timeout is None else timeout
        if duration <= 0:
            raise ValueError("timeout must be positive")
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        deadline = time.monotonic() + duration
        key = _id_key(request_id)
        self._pending.add(key)
        try:
            self._write_all(frame(message), deadline)
            return self._wait_response(request_id, deadline)
        finally:
            self._pending.discard(key)

    def initialize(self, params: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        supplied = dict(params) if params is not None else {
            "processId": os.getpid(),
            "clientInfo": {"name": "Elisa IDE"},
            "capabilities": {},
        }
        client_capabilities = supplied.setdefault("capabilities", {})
        if not isinstance(client_capabilities, dict):
            raise LspError("LSP client capabilities must be an object")
        workspace = client_capabilities.setdefault("workspace", {})
        if not isinstance(workspace, dict):
            raise LspError("LSP workspace client capabilities must be an object")
        workspace.setdefault("configuration", True)
        change_configuration = workspace.setdefault("didChangeConfiguration", {})
        if not isinstance(change_configuration, dict):
            raise LspError("LSP didChangeConfiguration client capability must be an object")
        change_configuration.setdefault("dynamicRegistration", True)
        workspace.setdefault("workspaceFolders", {"supported": True, "changeNotifications": True})
        response = self.request("initialize", supplied, timeout=timeout or self.startup_timeout)
        if "error" in response:
            return response
        result = response.get("result")
        if isinstance(result, dict):
            self.server_capabilities = ServerCapabilities.from_initialize(result)
            self.server_info = self.server_capabilities.server_info
            self.capabilities = self.server_capabilities.capabilities
            root_uri = supplied.get("rootUri")
            self.workspace_root_uri = root_uri if isinstance(root_uri, str) else None
            folders = supplied.get("workspaceFolders")
            if isinstance(folders, list):
                parsed_folders = [
                    item for item in folders
                    if isinstance(item, Mapping)
                    and isinstance(item.get("uri"), str)
                    and isinstance(item.get("name"), str)
                ]
                self.workspace_folder_uris = tuple(item["uri"] for item in parsed_folders)
                self.workspace_folder_names = {item["uri"]: item["name"] for item in parsed_folders}
        self.notify("initialized", {}, timeout=timeout or self.startup_timeout)
        return response

    def cancel(self, request_id: int | str) -> None:
        _id_key(request_id)
        self.notify("$/cancelRequest", {"id": request_id})

    def update_configuration(self, settings: Mapping[str, Any]) -> None:
        """Publish changed settings only after the server registers for them."""
        if not isinstance(settings, Mapping):
            raise TypeError("LSP configuration settings must be an object")
        if "workspace/didChangeConfiguration" not in self._registrations.values():
            raise LspError("LSP server has not registered for workspace/didChangeConfiguration")
        updated = dict(settings)
        previous = self.configuration
        self.configuration = updated
        try:
            self.notify("workspace/didChangeConfiguration", {"settings": updated})
        except Exception:
            self.configuration = previous
            raise

    def update_workspace_folders(
        self,
        *,
        added: Iterable[Mapping[str, str]] = (),
        removed: Iterable[str] = (),
    ) -> tuple[str, ...]:
        """Publish workspace folder changes when the server supports them."""
        if not (self.server_capabilities.workspace_folders_supported
                and self.server_capabilities.workspace_folder_change_notifications
                or "workspace/didChangeWorkspaceFolders" in self._registrations.values()):
            raise LspError("LSP server does not support workspace folder change notifications")
        added_items: list[dict[str, str]] = []
        for item in added:
            if not isinstance(item, Mapping):
                raise TypeError("added workspace folders must be objects with uri and name")
            uri, name = item.get("uri"), item.get("name")
            if not isinstance(uri, str) or not uri or not isinstance(name, str) or not name:
                raise ValueError("workspace folder uri and name must be nonempty strings")
            added_items.append({"uri": uri, "name": name})
        removed_uris = list(removed)
        if any(not isinstance(uri, str) or not uri for uri in removed_uris):
            raise ValueError("removed workspace folder URIs must be nonempty strings")
        if len({item["uri"] for item in added_items}) != len(added_items):
            raise ValueError("workspace folder additions contain duplicate URIs")
        if len(set(removed_uris)) != len(removed_uris):
            raise ValueError("workspace folder removals contain duplicate URIs")
        current = list(self.workspace_folder_uris)
        names = dict(self.workspace_folder_names)
        removed_items: list[dict[str, str]] = []
        for uri in removed_uris:
            if uri not in current:
                raise LspError(f"workspace folder is not open: {uri}")
            current.remove(uri)
            removed_items.append({"uri": uri, "name": names.pop(uri, uri)})
        for item in added_items:
            if item["uri"] in current:
                raise LspError(f"workspace folder is already open: {item['uri']}")
            current.append(item["uri"])
            names[item["uri"]] = item["name"]
        self.notify("workspace/didChangeWorkspaceFolders", {
            "event": {"added": added_items, "removed": removed_items}
        })
        self.workspace_folder_uris = tuple(current)
        self.workspace_folder_names = names
        return self.workspace_folder_uris

    def capability_request(self, capability: str, method: str, params: Any | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        """Issue a request only when the initialized server advertises it."""
        if not self.server_capabilities.supports(capability):
            raise LspError(f"LSP capability {capability!r} is unavailable")
        return self.request(method, params, timeout=timeout)

    def hover(self, uri: str, line: int, character: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "hoverProvider",
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
            timeout=timeout,
        )

    def signature_help(self, uri: str, line: int, character: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "signatureHelpProvider",
            "textDocument/signatureHelp",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
            timeout=timeout,
        )

    def document_symbols(self, uri: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "documentSymbolProvider",
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
            timeout=timeout,
        )

    def workspace_symbols(self, query: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request("workspaceSymbolProvider", "workspace/symbol", {"query": query}, timeout=timeout)

    def definition(self, uri: str, line: int, character: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "definitionProvider",
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
            timeout=timeout,
        )

    def references(self, uri: str, line: int, character: int, *, include_declaration: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "referencesProvider",
            "textDocument/references",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}, "context": {"includeDeclaration": include_declaration}},
            timeout=timeout,
        )

    def document_highlights(self, uri: str, line: int, character: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "documentHighlightProvider",
            "textDocument/documentHighlight",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
            timeout=timeout,
        )

    def folding_ranges(self, uri: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "foldingRangeProvider",
            "textDocument/foldingRange",
            {"textDocument": {"uri": uri}},
            timeout=timeout,
        )

    def selection_ranges(self, uri: str, positions: list[Mapping[str, int]], *, timeout: float | None = None) -> dict[str, Any]:
        return self.capability_request(
            "selectionRangeProvider",
            "textDocument/selectionRange",
            {"textDocument": {"uri": uri}, "positions": positions},
            timeout=timeout,
        )

    def semantic_tokens(self, uri: str, *, result_id: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"textDocument": {"uri": uri}}
        method = "textDocument/semanticTokens/full"
        if result_id is not None:
            params["previousResultId"] = result_id
            method = "textDocument/semanticTokens/full/delta"
        return self.capability_request("semanticTokensProvider", method, params, timeout=timeout)

    def open_document(self, uri: str, text: str, *, version: int = 1, language_id: str = "elisa") -> None:
        if uri in self.documents:
            raise LspError(f"document is already open: {uri}")
        if type(version) is not int or version < 0:
            raise LspError("document versions must be nonnegative integers")
        self.documents[uri] = (text, version)
        self.diagnostics.set_document_version(uri, version)
        if self.server_capabilities.document_open_close is not False:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language_id, "version": version, "text": text}})

    @staticmethod
    def _character_units(text: str, encoding: str) -> int:
        if encoding == "utf-8":
            return len(text.encode("utf-8"))
        if encoding == "utf-16":
            return len(text.encode("utf-16-le")) // 2
        if encoding == "utf-32":
            return len(text)
        raise LspError(f"unsupported LSP position encoding {encoding!r}")

    def _end_position(self, text: str) -> dict[str, int]:
        encoding = self.server_capabilities.position_encoding
        if encoding is None:
            raise LspError("LSP server did not negotiate a position encoding")
        lines = text.split("\n")
        return {"line": len(lines) - 1, "character": self._character_units(lines[-1], encoding)}

    def change_document(self, uri: str, text: str, *, version: int | None = None) -> int:
        current = self.documents.get(uri)
        if current is None:
            raise LspError(f"document is not open: {uri}")
        if version is not None and type(version) is not int:
            raise LspError("document versions must be integers")
        next_version = current[1] + 1 if version is None else version
        if next_version <= current[1]:
            raise LspError("document versions must increase monotonically")
        sync_kind = self.server_capabilities.document_sync_kind
        if sync_kind is None:
            raise LspError("LSP server did not advertise text document synchronization")
        if sync_kind == 0:
            raise LspError("LSP server does not support document changes")
        change: dict[str, Any] = {"text": text}
        if sync_kind == 2:
            change["range"] = {"start": {"line": 0, "character": 0}, "end": self._end_position(current[0])}
        self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": next_version}, "contentChanges": [change]})
        self.documents[uri] = (text, next_version)
        self.diagnostics.set_document_version(uri, next_version)
        return next_version

    def save_document(self, uri: str) -> None:
        if uri not in self.documents:
            raise LspError(f"document is not open: {uri}")
        self.notify("textDocument/didSave", {"textDocument": {"uri": uri}})

    def close_document(self, uri: str) -> None:
        if uri not in self.documents:
            return
        if self.server_capabilities.document_open_close is not False:
            self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self.documents.pop(uri, None)
        self.diagnostics.remove(uri)

    def notification(self, method: str) -> dict[str, Any] | None:
        for index, item in enumerate(self._notifications):
            if item.get("method") == method:
                return self._notifications.pop(index)
        return None

    def wait_notification(self, method: str, *, timeout: float | None = None) -> dict[str, Any]:
        duration = self.request_timeout if timeout is None else timeout
        deadline = time.monotonic() + duration
        while True:
            found = self.notification(method)
            if found is not None:
                return found
            if self._stdout_eof:
                raise LspError(f"LSP stdout closed before {method!r}")
            try:
                self._read_frames(deadline)
            except LspError:
                self.abort()
                raise

    def shutdown(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self.request("shutdown", timeout=timeout)

    def close(self, *, expect_exit: int = 0, timeout: float | None = None) -> int:
        if self._closed:
            return self.process.returncode if self.process and self.process.returncode is not None else 0
        if self.process is None:
            self._closed = True
            return 0
        duration = self.teardown_timeout if timeout is None else timeout
        deadline = time.monotonic() + duration
        if self.process.poll() is None:
            try:
                self.notify("exit", {}, timeout=min(duration, self.request_timeout))
            except LspError:
                self.abort()
                raise
            try:
                self.process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self.abort()
                raise LspError("LSP server did not exit before teardown deadline")
        status = self.process.returncode
        if status != expect_exit:
            details = self.stderr_text()[:2000]
            self.abort()
            raise LspError(f"LSP server exit code {status} != {expect_exit}; stderr={details!r}")
        self._closed = True
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
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
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
        self._closed = True
