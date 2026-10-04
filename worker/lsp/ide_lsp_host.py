#!/usr/bin/env python3
"""Line-protocol bridge from the native IDE host to Elisa-LSP workspaces.

The native shell owns source buffers; its process bridge sends complete,
versioned snapshots to this sidecar when that integration is connected. All
request fields are UTF-8 hex so paths and source text cannot break framing.
Responses are bounded tab-separated records, also with text fields hex encoded.
The sidecar never reads a source file itself; the editor's unsaved buffer is
authoritative.

Commands:
  OPEN <absolute-path-hex> <source-utf8-hex>
  CHANGE <absolute-path-hex> <source-utf8-hex>
  DEFINITION <absolute-path-hex> <line:character-utf8-hex>
  SYMBOLS <absolute-path-hex> <query-utf8-hex>
  SYMBOLS <absolute-path-hex> <query-utf8-hex>
  CLOSE <absolute-path-hex>
  RESTART
  POLL
  SHUTDOWN

Each command returns one batch terminated by ``Z``. ``S`` records report a
workspace/server generation and negotiated encoding. ``G`` records delimit a
document's versioned diagnostic group; ``P`` records carry diagnostic fields
and are followed by zero or more ``R`` related-information records.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(ROOT / "src"))

from lsp import LspError, LspSupervisor, SupervisorState, resolve_server  # noqa: E402
from lsp.workspace_service import LspWorkspace  # noqa: E402
from source import SourceEdit  # noqa: E402

MAX_SOURCE_BYTES = 1 << 20
MAX_REQUEST_LINE_BYTES = (MAX_SOURCE_BYTES * 2) + 8192
MAX_DIAGNOSTICS_PER_DOCUMENT = 128
MAX_WORKSPACE_SYMBOLS = 16
MAX_FIELD_BYTES = 4096
MAX_OUTPUT_BATCH_BYTES = 2 << 20


def _hex_decode(value: str, *, limit: int) -> str:
    if len(value) > limit * 2 or len(value) % 2:
        raise ValueError("hex field is malformed or exceeds its byte limit")
    if any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("hex field contains a non-hexadecimal character")
    try:
        raw = bytes.fromhex(value)
        decoded = raw.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("hex field is not valid UTF-8") from exc
    if b"\0" in raw:
        raise ValueError("text fields may not contain NUL")
    return decoded


def _hex_encode(value: str | None, *, limit: int = MAX_FIELD_BYTES) -> str:
    if value is None:
        return ""
    encoded = value.encode("utf-8")[:limit]
    while encoded:
        try:
            encoded.decode("utf-8", errors="strict")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return encoded.hex()


def _project_root(source_path: Path) -> Path:
    """Use the nearest regular project manifest, else the source file's folder."""
    parent = source_path.parent
    for candidate in (parent, *parent.parents):
        manifest = candidate / ".elisaproject.json"
        try:
            if manifest.is_file() and not manifest.is_symlink():
                return candidate
        except OSError:
            continue
    return parent


@dataclass
class WorkspaceState:
    root: Path
    buffers: dict[str, tuple[str, str]] = field(default_factory=dict)
    supervisor: LspSupervisor | None = None
    workspace: LspWorkspace | None = None
    selected_by: str = ""
    server_path: str = ""
    error: str = ""


class IdeLspHost:
    def __init__(self, environment: dict[str, str] | None = None):
        self.environment = dict(os.environ if environment is None else environment)
        self.workspaces: dict[str, WorkspaceState] = {}
        self.documents: dict[str, str] = {}
        self.stopping = False

    def _state_for(self, source_path: Path) -> WorkspaceState:
        root = _project_root(source_path)
        key = os.fspath(root)
        if key not in self.workspaces:
            self.workspaces[key] = WorkspaceState(root=root)
        return self.workspaces[key]

    def _start(self, state: WorkspaceState, *, restart: bool = False) -> bool:
        try:
            if state.workspace is not None and state.supervisor is not None:
                if restart:
                    state.workspace.restart()
                elif state.supervisor.state == SupervisorState.RUNNING:
                    return True
                else:
                    state.workspace.restart()
            else:
                server = resolve_server(state.root, env=self.environment)
                state.selected_by = server.selected_by
                state.server_path = os.fspath(server.path)
                state.supervisor = LspSupervisor(
                    server.command,
                    cwd=state.root,
                    env=self.environment,
                    initialize_params={
                        "rootUri": state.root.as_uri(),
                        "workspaceFolders": [{"uri": state.root.as_uri(), "name": state.root.name}],
                    },
                    max_restarts=3,
                    restart_window_seconds=60.0,
                    client_options={"startup_timeout": 5.0, "request_timeout": 5.0},
                )
                state.workspace = LspWorkspace(state.supervisor)
                state.workspace.start()
                for uri, (path, text) in state.buffers.items():
                    state.workspace.open_document(uri, text, language_id="elisa")
            state.error = ""
            return True
        except Exception as exc:
            state.error = str(exc)[:MAX_FIELD_BYTES]
            return False

    def open_document(self, path_text: str, source_text: str) -> None:
        source_path = Path(path_text).expanduser().resolve(strict=False)
        if not source_path.is_absolute() or source_path.suffix != ".elisa":
            raise ValueError("LSP source path must be an absolute .elisa path")
        if len(source_text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("LSP source exceeds the 1 MiB buffer limit")
        uri = source_path.as_uri()
        state = self._state_for(source_path)
        if uri in self.documents:
            self.change_document(path_text, source_text)
            return
        state.buffers[uri] = (os.fspath(source_path), source_text)
        self.documents[uri] = os.fspath(state.root)
        if self._start(state):
            # A freshly started workspace replays every retained editor buffer
            # in _start; a running workspace only needs the newly opened URI.
            if state.workspace is not None and uri not in state.workspace.buffers:
                try:
                    state.workspace.open_document(uri, source_text, language_id="elisa")
                except LspError as exc:
                    state.error = str(exc)[:MAX_FIELD_BYTES]

    def change_document(self, path_text: str, source_text: str) -> None:
        source_path = Path(path_text).expanduser().resolve(strict=False)
        uri = source_path.as_uri()
        if uri not in self.documents:
            self.open_document(path_text, source_text)
            return
        if len(source_text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("LSP source exceeds the 1 MiB buffer limit")
        state = self.workspaces[self.documents[uri]]
        previous_text = state.buffers[uri][1]
        state.buffers[uri] = (os.fspath(source_path), source_text)
        if state.workspace is None:
            self._start(state)
            return
        buffer = state.workspace.buffers.get(uri)
        if buffer is None:
            self._start(state)
            if state.workspace is not None and uri not in state.workspace.buffers:
                try:
                    state.workspace.open_document(uri, source_text, language_id="elisa")
                except LspError as exc:
                    state.error = str(exc)[:MAX_FIELD_BYTES]
            return
        try:
            state.workspace.apply_edit(
                uri,
                SourceEdit.text(0, len(previous_text.encode("utf-8")), source_text),
            )
            state.error = buffer.sync_error or ""
        except (LspError, ValueError) as exc:
            state.error = str(exc)[:MAX_FIELD_BYTES]

    def definition(self, path_text: str, line: int, character: int) -> str:
        """Return one bounded definition location for an open source buffer."""
        source_path = Path(path_text).expanduser().resolve(strict=False)
        if not source_path.is_absolute() or source_path.suffix != ".elisa":
            raise ValueError("LSP definition path must be an absolute .elisa path")
        if line < 0 or character < 0 or line > 100_000_000 or character > 100_000_000:
            raise ValueError("LSP definition position is outside the supported range")
        uri = source_path.as_uri()
        root_key = self.documents.get(uri)
        state = self.workspaces.get(root_key) if root_key is not None else None
        if state is None or state.workspace is None:
            raise LspError("LSP definition source is not open")
        try:
            result = state.workspace.definition(uri, line, character)
        except LspError:
            # Unsupported capabilities and server-side request failures both
            # produce an explicit no-result record; the editor remains usable.
            return "N"
        candidates = result if isinstance(result, list) else [result]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            target_uri = candidate.get("uri", candidate.get("targetUri"))
            target_range = candidate.get("range")
            if not isinstance(target_range, dict):
                target_range = candidate.get("targetSelectionRange", candidate.get("targetRange"))
            start = target_range.get("start") if isinstance(target_range, dict) else None
            target_line = start.get("line") if isinstance(start, dict) else None
            target_character = start.get("character") if isinstance(start, dict) else None
            if (isinstance(target_uri, str) and target_uri
                    and type(target_line) is int and target_line >= 0
                    and type(target_character) is int and target_character >= 0):
                return "\t".join((
                    "D",
                    str(target_line),
                    str(target_character),
                    _hex_encode(target_uri, limit=4096),
                ))
        return "N"

    def workspace_symbols(self, path_text: str, query: str) -> list[str]:
        """Return bounded local Elisa locations from an advertised provider."""
        source_path = Path(path_text).expanduser().resolve(strict=False)
        if not source_path.is_absolute() or source_path.suffix != ".elisa":
            raise ValueError("LSP symbol search path must be an absolute .elisa path")
        if not query or len(query.encode("utf-8")) > 256:
            raise ValueError("LSP symbol query must contain 1 to 256 UTF-8 bytes")
        uri = source_path.as_uri()
        root_key = self.documents.get(uri)
        state = self.workspaces.get(root_key) if root_key is not None else None
        if state is None or state.workspace is None:
            raise LspError("LSP symbol search source is not open")
        try:
            result = state.workspace.workspace_symbols(uri, query)
        except (LspError, ValueError):
            return []
        if not isinstance(result, list):
            return []
        records: list[str] = []
        for symbol in result:
            if not isinstance(symbol, dict):
                continue
            location = symbol.get("location")
            target_uri = location.get("uri") if isinstance(location, dict) else None
            target_range = location.get("range") if isinstance(location, dict) else None
            if not isinstance(target_uri, str):
                target_uri = symbol.get("targetUri")
            if not isinstance(target_range, dict):
                target_range = symbol.get("targetSelectionRange", symbol.get("targetRange"))
            start = target_range.get("start") if isinstance(target_range, dict) else None
            line = start.get("line") if isinstance(start, dict) else None
            character = start.get("character") if isinstance(start, dict) else None
            name = symbol.get("name")
            kind = symbol.get("kind", 0)
            if (not isinstance(target_uri, str) or type(line) is not int or line < 0
                    or type(character) is not int or character < 0
                    or type(kind) is not int or kind < 0 or kind > 255
                    or not isinstance(name, str) or not name
                    or len(name.encode("utf-8")) > 128):
                continue
            try:
                parsed_uri = urlsplit(target_uri)
                if parsed_uri.scheme != "file" or parsed_uri.netloc not in ("", "localhost"):
                    continue
                target_path = Path(unquote(parsed_uri.path)).resolve(strict=False)
                if not target_path.is_absolute() or target_path.suffix != ".elisa":
                    continue
                if not target_path.is_relative_to(state.root.resolve(strict=False)):
                    continue
                if len(os.fsencode(target_path)) > 4096:
                    continue
            except (ValueError, OSError):
                continue
            container = symbol.get("containerName", "")
            if not isinstance(container, str):
                container = ""
            records.append("\t".join((
                "W", str(kind), str(line), str(character),
                _hex_encode(name, limit=128), _hex_encode(container, limit=128),
                _hex_encode(target_uri, limit=4096),
            )))
            if len(records) >= MAX_WORKSPACE_SYMBOLS:
                break
        return records

    def close_document(self, path_text: str) -> None:
        source_path = Path(path_text).expanduser().resolve(strict=False)
        uri = source_path.as_uri()
        root_key = self.documents.pop(uri, None)
        if root_key is None:
            return
        state = self.workspaces[root_key]
        state.buffers.pop(uri, None)
        if state.workspace is not None:
            state.workspace.close_document(uri)
        if not state.buffers:
            if state.supervisor is not None:
                try:
                    state.supervisor.close()
                except LspError as exc:
                    state.error = str(exc)[:MAX_FIELD_BYTES]
            state.supervisor = None
            state.workspace = None
            state.selected_by = ""
            state.server_path = ""

    def restart(self) -> None:
        for state in self.workspaces.values():
            self._start(state, restart=True)

    def _status_record(self, state: WorkspaceState) -> str:
        supervisor = state.supervisor
        status = supervisor.state.value if supervisor is not None else "unavailable"
        generation = supervisor.generation if supervisor is not None else 0
        encoding = state.workspace.position_encoding if state.workspace is not None else ""
        return "\t".join(
            (
                "S",
                _hex_encode(os.fspath(state.root)),
                status,
                str(generation),
                _hex_encode(state.selected_by),
                _hex_encode(state.server_path),
                _hex_encode(encoding),
                _hex_encode(state.error),
                "1" if supervisor is not None and supervisor.capabilities.supports("workspaceSymbolProvider") else "0",
            )
        )

    def _document_records(self, state: WorkspaceState, uri: str) -> list[str]:
        supervisor = state.supervisor
        workspace = state.workspace
        buffer = workspace.buffers.get(uri) if workspace is not None else None
        if buffer is not None:
            workspace.refresh_problems(uri)
            version = buffer.lsp_version
        else:
            version = 0
        typed = (
            supervisor.diagnostics.typed(uri)
            if supervisor is not None and buffer is not None and buffer.synced_revision == buffer.document.revision
            else ()
        )
        current = tuple(item for item in typed if item.version == version)
        records = [f"G\t{_hex_encode(uri)}\t{version}\t{min(len(current), MAX_DIAGNOSTICS_PER_DOCUMENT)}"]
        for item in current[:MAX_DIAGNOSTICS_PER_DOCUMENT]:
            severity = item.severity if item.severity is not None else 3
            code = str(item.code) if item.code is not None else ""
            fields = (
                "P",
                str(severity),
                str(item.start_line if item.start_line is not None else -1),
                str(item.start_character if item.start_character is not None else -1),
                str(item.end_line if item.end_line is not None else -1),
                str(item.end_character if item.end_character is not None else -1),
                _hex_encode(code),
                _hex_encode(item.source),
                _hex_encode(item.message),
                str(len(item.related_information)),
            )
            records.append("\t".join(fields))
            for related in item.related_information:
                location = related.get("location", {})
                related_uri = location.get("uri", "") if isinstance(location, dict) else ""
                related_range = location.get("range", {}) if isinstance(location, dict) else {}
                start = related_range.get("start", {}) if isinstance(related_range, dict) else {}
                end = related_range.get("end", {}) if isinstance(related_range, dict) else {}
                records.append(
                    "\t".join(
                        (
                            "R",
                            _hex_encode(related_uri),
                            str(start.get("line", -1)),
                            str(start.get("character", -1)),
                            str(end.get("line", -1)),
                            str(end.get("character", -1)),
                            _hex_encode(related.get("message", "")),
                        )
                    )
                )
        return records

    def response_records(self) -> list[str]:
        records: list[str] = []
        for state in self.workspaces.values():
            if state.supervisor is not None:
                try:
                    state.supervisor.poll_notifications(timeout=0.0)
                except LspError as exc:
                    state.error = str(exc)[:MAX_FIELD_BYTES]
                if not state.supervisor.check_health():
                    failure = state.supervisor.last_failure
                    if failure is not None:
                        state.error = failure.message[:MAX_FIELD_BYTES]
                if state.workspace is not None:
                    state.workspace.refresh_problems()
            records.append(self._status_record(state))
            for uri in state.buffers:
                records.extend(self._document_records(state, uri))
        encoded_size = sum(len(record.encode("utf-8")) + 1 for record in records)
        if encoded_size > MAX_OUTPUT_BATCH_BYTES:
            raise ValueError("LSP response exceeds the 2 MiB batch limit")
        return records

    def close(self) -> None:
        self.stopping = True
        for state in self.workspaces.values():
            if state.supervisor is not None:
                try:
                    state.supervisor.close()
                except LspError:
                    state.supervisor.abort()


def _parse_command(line: bytes) -> tuple[str, str, str]:
    if not line.endswith(b"\n") or len(line) > MAX_REQUEST_LINE_BYTES:
        raise ValueError("LSP command is incomplete or exceeds its line limit")
    try:
        command_line = line[:-1].decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("LSP command framing must be ASCII") from exc
    parts = command_line.split(" ", 2)
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) != 3:
        raise ValueError("LSP command fields are malformed")
    return parts[0], parts[1], parts[2]


def run(source: TextIO = sys.stdin, sink: TextIO = sys.stdout) -> int:
    # TextIO is accepted for tests; byte framing below always uses the actual
    # stdio buffers so invalid input cannot be silently replaced by a decoder.
    del source, sink
    host = IdeLspHost()
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    while not host.stopping:
        line = reader.readline(MAX_REQUEST_LINE_BYTES + 1)
        if not line:
            break
        try:
            command, first, second = _parse_command(line)
            extra_records: list[str] = []
            if command == "OPEN":
                path = _hex_decode(first, limit=4096)
                text = _hex_decode(second, limit=MAX_SOURCE_BYTES)
                host.open_document(path, text)
            elif command == "CHANGE":
                path = _hex_decode(first, limit=4096)
                text = _hex_decode(second, limit=MAX_SOURCE_BYTES)
                host.change_document(path, text)
            elif command == "DEFINITION":
                path = _hex_decode(first, limit=4096)
                position = _hex_decode(second, limit=64)
                fields = position.split(":")
                if len(fields) != 2 or any(not field.isdecimal() for field in fields):
                    raise ValueError("LSP definition position is malformed")
                extra_records.append(host.definition(path, int(fields[0]), int(fields[1])))
            elif command == "SYMBOLS":
                path = _hex_decode(first, limit=4096)
                query = _hex_decode(second, limit=256)
                symbol_records = host.workspace_symbols(path, query)
                extra_records.extend(symbol_records)
                extra_records.append(f"Q\t{len(symbol_records)}")
            elif command == "CLOSE":
                host.close_document(_hex_decode(first, limit=4096))
            elif command == "RESTART":
                host.restart()
            elif command == "POLL":
                pass
            elif command == "SHUTDOWN":
                host.close()
            else:
                raise ValueError("unknown LSP command")
            records = host.response_records()
            records.extend(extra_records)
        except Exception as exc:
            records = ["E\t" + _hex_encode(str(exc))]
        records.append("Z")
        response = ("\n".join(records) + "\n").encode("ascii")
        if len(response) > MAX_OUTPUT_BATCH_BYTES:
            response = ("E\t" + _hex_encode("LSP response exceeds the batch limit") + "\nZ\n").encode("ascii")
        writer.write(response)
        writer.flush()
        if host.stopping:
            break
    host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
