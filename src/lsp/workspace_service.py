"""Workspace-owned source buffers synchronized with an Elisa language server.

The protocol client, source buffer, and shared Problems projection each have
separate responsibilities.  This service is the lifecycle seam between them:
it keeps unsaved UTF-8 buffers alive when the server is unavailable, syncs
monotonic document revisions, drops stale diagnostics immediately, and maps
current diagnostics back to exact source byte ranges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from problems import Problem, ProblemModel, ProblemSource
from source import SourceDocument, SourceEdit

from .lsp_client import LspError
from .lsp_supervisor import LspSupervisor, SupervisorState


@dataclass
class WorkspaceBuffer:
    """One open Elisa buffer and the revision last accepted by the server."""

    uri: str
    document: SourceDocument
    language_id: str
    lsp_version: int
    synced_revision: int | None = None
    sync_error: str | None = None


class LspWorkspace:
    """Coordinate open source buffers, LSP synchronization, and Problems."""

    def __init__(self, supervisor: LspSupervisor, problems: ProblemModel | None = None):
        self.supervisor = supervisor
        self.problems = problems if problems is not None else ProblemModel()
        self.buffers: dict[str, WorkspaceBuffer] = {}
        self.position_encoding: str | None = None
        self._last_versions: dict[str, int] = {}

    def start(self) -> dict[str, object]:
        """Start the server, then open any buffers created while it was down."""
        response = self.supervisor.start()
        self._remember_capabilities()
        for buffer in tuple(self.buffers.values()):
            self._synchronize(buffer)
        return response

    def open_document(
        self,
        uri: str,
        data: bytes | bytearray | memoryview | str,
        *,
        language_id: str = "elisa",
    ) -> WorkspaceBuffer:
        if not isinstance(uri, str) or not uri:
            raise ValueError("document URI must be nonempty text")
        if not isinstance(language_id, str) or not language_id:
            raise ValueError("language ID must be nonempty text")
        if uri in self.buffers:
            raise LspError(f"document is already open in the workspace: {uri}")
        version = self._last_versions.get(uri, 0) + 1
        self._last_versions[uri] = version
        buffer = WorkspaceBuffer(uri, SourceDocument(data), language_id, version)
        self.buffers[uri] = buffer
        self._synchronize(buffer)
        self.refresh_problems(uri)
        return buffer

    def close_document(self, uri: str) -> bool:
        buffer = self.buffers.pop(uri, None)
        if buffer is None:
            return False
        try:
            self.supervisor.close_document(uri)
        except LspError as exc:
            # Closing an editor tab and clearing its diagnostics must still
            # work while the language server is down.
            buffer.sync_error = str(exc)
        self.problems.clear_group(ProblemSource.LSP, uri, revision=buffer.lsp_version)
        return True

    def apply_edit(self, uri: str, edit: SourceEdit) -> int:
        buffer = self._buffer(uri)
        previous_revision = buffer.document.revision
        revision = buffer.document.apply_edit(edit)
        self._after_source_change(buffer, revision != previous_revision)
        return revision

    def find_all(
        self,
        uri: str,
        query: str,
        *,
        start_byte: int = 0,
        max_matches: int = 10_000,
    ) -> tuple[tuple[int, int], ...]:
        return self._buffer(uri).document.find_all(
            query, start_byte=start_byte, max_matches=max_matches
        )

    def replace_all(
        self,
        uri: str,
        query: str,
        replacement: str,
        *,
        max_matches: int = 10_000,
    ) -> int:
        buffer = self._buffer(uri)
        count = buffer.document.replace_all(query, replacement, max_matches=max_matches)
        self._after_source_change(buffer, count > 0)
        return count

    def definition(self, uri: str, line: int, character: int) -> object:
        """Request a definition for the current synchronized buffer snapshot."""
        buffer = self._buffer(uri)
        if buffer.synced_revision != buffer.document.revision:
            raise LspError(f"document is not synchronized with Elisa-LSP: {uri}")
        if self.supervisor.state != SupervisorState.RUNNING:
            raise LspError("Elisa-LSP is not running")
        if not self.supervisor.capabilities.supports("definitionProvider"):
            raise LspError("Elisa-LSP does not support definition requests")
        return self.supervisor.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            },
        ).get("result")

    def workspace_symbols(self, uri: str, query: str = "") -> object:
        """Search the synchronized workspace when the server advertises it."""
        buffer = self._buffer(uri)
        if buffer.synced_revision != buffer.document.revision:
            raise LspError(f"document is not synchronized with Elisa-LSP: {uri}")
        if self.supervisor.state != SupervisorState.RUNNING:
            raise LspError("Elisa-LSP is not running")
        if not self.supervisor.capabilities.supports("workspaceSymbolProvider"):
            raise LspError("Elisa-LSP does not support workspace symbol requests")
        if len(query.encode("utf-8")) > 256:
            raise ValueError("workspace symbol query exceeds 256 UTF-8 bytes")
        result = self.supervisor.request("workspace/symbol", {"query": query}).get("result")
        if result is not None and not isinstance(result, list):
            raise LspError("Elisa-LSP returned a malformed workspace symbol result")
        return result

    def indent_lines(
        self,
        uri: str,
        first_line: int,
        last_line: int,
        indentation: str = "    ",
    ) -> bool:
        buffer = self._buffer(uri)
        changed = buffer.document.indent_lines(first_line, last_line, indentation)
        self._after_source_change(buffer, changed)
        return changed

    def outdent_lines(
        self,
        uri: str,
        first_line: int,
        last_line: int,
        indentation: str = "    ",
    ) -> bool:
        buffer = self._buffer(uri)
        changed = buffer.document.outdent_lines(first_line, last_line, indentation)
        self._after_source_change(buffer, changed)
        return changed

    def undo(self, uri: str) -> bool:
        buffer = self._buffer(uri)
        if not buffer.document.undo():
            return False
        self._after_source_change(buffer, True)
        return True

    def redo(self, uri: str) -> bool:
        buffer = self._buffer(uri)
        if not buffer.document.redo():
            return False
        self._after_source_change(buffer, True)
        return True

    def restart(self) -> dict[str, object]:
        """Restart the server and advance any replayed older buffers."""
        response = self.supervisor.restart()
        self._remember_capabilities()
        for buffer in tuple(self.buffers.values()):
            self._synchronize(buffer)
        return response

    def refresh_problems(self, uri: str | None = None) -> int:
        """Project only diagnostics matching each open buffer's revision."""
        targets: Iterable[WorkspaceBuffer]
        if uri is None:
            targets = tuple(self.buffers.values())
        else:
            buffer = self._buffer(uri)
            targets = (buffer,)
        updated = 0
        for buffer in targets:
            current = tuple(
                item
                for item in self.supervisor.diagnostics.typed(buffer.uri)
                if item.version == buffer.lsp_version
                and buffer.synced_revision == buffer.document.revision
            )
            projected = tuple(
                Problem.from_lsp(
                    item,
                    document=buffer.document,
                    encoding=self.position_encoding,
                    revision=buffer.document.revision,
                )
                for item in current
            )
            if self.problems.replace_group(
                ProblemSource.LSP,
                buffer.uri,
                projected,
                revision=buffer.lsp_version,
            ):
                updated += 1
        return updated

    def _synchronize(self, buffer: WorkspaceBuffer) -> bool:
        buffer.sync_error = None
        try:
            open_documents = self.supervisor.documents
            if buffer.uri not in open_documents:
                self.supervisor.open_document(
                    buffer.uri,
                    buffer.document.text,
                    version=buffer.lsp_version,
                    language_id=buffer.language_id,
                )
            else:
                opened = open_documents[buffer.uri]
                if buffer.synced_revision != buffer.document.revision:
                    if buffer.lsp_version <= opened.version:
                        buffer.lsp_version = opened.version + 1
                        self._last_versions[buffer.uri] = buffer.lsp_version
                    self.supervisor.change_document(
                        buffer.uri,
                        buffer.document.text,
                        version=buffer.lsp_version,
                    )
            buffer.synced_revision = buffer.document.revision
            self._remember_capabilities()
            return True
        except LspError as exc:
            buffer.sync_error = str(exc)
            return False

    def _remember_capabilities(self) -> None:
        if self.supervisor.state == SupervisorState.RUNNING:
            self.position_encoding = self.supervisor.capabilities.position_encoding

    def _advance_lsp_version(self, buffer: WorkspaceBuffer) -> None:
        version = max(self._last_versions.get(buffer.uri, 0), buffer.lsp_version) + 1
        buffer.lsp_version = version
        self._last_versions[buffer.uri] = version

    def _after_source_change(self, buffer: WorkspaceBuffer, changed: bool) -> None:
        if not changed:
            return
        self._advance_lsp_version(buffer)
        self.problems.clear_group(
            ProblemSource.LSP, buffer.uri, revision=buffer.lsp_version
        )
        self._synchronize(buffer)

    def _buffer(self, uri: str) -> WorkspaceBuffer:
        try:
            return self.buffers[uri]
        except KeyError as exc:
            raise LspError(f"document is not open in the workspace: {uri}") from exc
