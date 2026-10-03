"""Elisa IDE language-service integration primitives."""

from .lsp_client import (
    DiagnosticStore,
    PublishedDiagnostic,
    FrameDecoder,
    FrameError,
    LspClient,
    LspError,
    ServerCapabilities,
    path_to_uri,
)
from .server_resolution import LspResolutionError, LspServer, resolve_server
from .lsp_supervisor import LspFailure, LspSupervisor, OpenDocument, SupervisorState

__all__ = [
    "DiagnosticStore",
    "PublishedDiagnostic",
    "FrameDecoder",
    "FrameError",
    "LspClient",
    "LspError",
    "ServerCapabilities",
    "path_to_uri",
    "LspResolutionError",
    "LspServer",
    "resolve_server",
    "LspFailure",
    "LspSupervisor",
    "OpenDocument",
    "SupervisorState",
]
