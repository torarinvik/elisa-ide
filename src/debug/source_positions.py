"""Explicit coordinate conversion at the Elisa EDIR/DAP boundary.

Compiler EDIR spans use one-based lines and one-based UTF-8 byte columns.
The DAP client negotiates one-based lines and zero-based UTF-16 columns.
Conversions use the exact source snapshot so multibyte scalars, surrogate
pairs, combining marks, tabs, and CRLF line endings are handled by the same
source-buffer rules used by the editor and LSP integration.
"""

from __future__ import annotations

from source.source_document import PositionError, SourceDocument


def edir_to_dap_position(
    document: SourceDocument,
    line: int,
    byte_column: int,
) -> tuple[int, int]:
    """Convert an EDIR one-based UTF-8 source span to DAP coordinates."""

    if type(line) is not int or type(byte_column) is not int or line < 1 or byte_column < 1:
        raise PositionError("EDIR positions use positive one-based line and byte-column values")
    byte_offset = document.position_to_byte(line - 1, byte_column - 1, encoding="utf-8")
    dap_line, dap_column = document.byte_to_position(byte_offset, encoding="utf-16")
    return dap_line + 1, dap_column


def dap_to_edir_position(
    document: SourceDocument,
    line: int,
    utf16_column: int,
) -> tuple[int, int]:
    """Convert negotiated DAP one-based-line/zero-based-UTF-16 positions to EDIR."""

    if type(line) is not int or type(utf16_column) is not int or line < 1 or utf16_column < 0:
        raise PositionError("DAP positions require a positive line and nonnegative UTF-16 column")
    byte_offset = document.position_to_byte(line - 1, utf16_column, encoding="utf-16")
    edir_line, edir_column = document.byte_to_position(byte_offset, encoding="utf-8")
    return edir_line + 1, edir_column + 1


__all__ = ["dap_to_edir_position", "edir_to_dap_position"]
