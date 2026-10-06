"""Debug adapter transport primitives for Elisa IDE."""

from .dap_client import DapClient, DapError, DapProtocolError, DapSessionIdentity
from .dap_session import (
    BreakpointResult,
    DebugArtifact,
    DebugSessionError,
    DebugSessionState,
    ElisaDebugSession,
    inspect_debug_artifact,
    resolve_debug_adapter,
)
from .source_positions import dap_to_edir_position, edir_to_dap_position

__all__ = [
    "BreakpointResult",
    "DapClient",
    "DapError",
    "DapProtocolError",
    "DapSessionIdentity",
    "DebugArtifact",
    "DebugSessionError",
    "DebugSessionState",
    "ElisaDebugSession",
    "inspect_debug_artifact",
    "resolve_debug_adapter",
    "dap_to_edir_position",
    "edir_to_dap_position",
]
