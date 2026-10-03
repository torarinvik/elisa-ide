"""Bounded parsing of compiler and package-manager diagnostics.

Build output remains an exact, bounded byte log in :mod:`build_job`.  This
module adds a separate typed projection for lines that carry a source
location, while retaining each parsed line verbatim so a UI can always show
the original tool output.  Unknown lines are deliberately ignored by the
projection and remain available in the raw task log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"
    INFO = "info"


@dataclass(frozen=True)
class Diagnostic:
    """One parsed tool diagnostic, with its original line preserved."""

    raw: str
    message: str
    severity: DiagnosticSeverity
    source: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    code: str | None = None

    @property
    def has_location(self) -> bool:
        return self.path is not None and self.line is not None


_SEVERITY = r"(?:fatal\s+error|error|warning|note|info)"
_SEVERITY_RE = re.compile(rf"^(?P<severity>{_SEVERITY})(?:\[(?P<code>[^\]\s]+)\])?\s*:\s*(?P<body>.*)$", re.IGNORECASE)
_LOCATION_RE = re.compile(
    rf"^(?P<path>.+?):(?P<line>[0-9]+)(?::(?P<column>[0-9]+))?:\s*"
    rf"(?:(?P<severity>{_SEVERITY})(?:\[(?P<code>[^\]\s]+)\])?\s*:\s*)?"
    rf"(?P<message>.*)$",
    re.IGNORECASE,
)
_PAREN_LOCATION_RE = re.compile(
    rf"^(?P<path>.+?)\((?P<line>[0-9]+)(?:,(?P<column>[0-9]+))?\):\s*"
    rf"(?:(?P<severity>{_SEVERITY})(?:\[(?P<code>[^\]\s]+)\])?\s*:\s*)?"
    rf"(?P<message>.*)$",
    re.IGNORECASE,
)
_CODE_PREFIX_RE = re.compile(r"^\[([^\]\s]+)\]\s*(.*)$")


def _severity(value: str | None) -> DiagnosticSeverity:
    normalized = (value or "error").strip().lower()
    if normalized == "fatal error":
        return DiagnosticSeverity.ERROR
    try:
        return DiagnosticSeverity(normalized)
    except ValueError:
        return DiagnosticSeverity.ERROR


def _number(value: str | None) -> int | None:
    if value is None:
        return None
    number = int(value)
    # Compiler coordinates are one-based. Reject zero rather than silently
    # inventing a valid source location; callers can still display raw text.
    return number if number > 0 else None


def _code_and_message(message: str, code: str | None) -> tuple[str | None, str]:
    match = _CODE_PREFIX_RE.match(message.strip())
    if match:
        code = code or match.group(1)
        message = match.group(2)
    return code, message.strip()


def _parse_line(raw: str, source: str) -> Diagnostic | None:
    text = raw.strip()
    if not text:
        return None

    location = _LOCATION_RE.match(text) or _PAREN_LOCATION_RE.match(text)
    if location:
        groups = location.groupdict()
        path = groups.get("path", "").strip()
        if not path:
            return None
        code, message = _code_and_message(groups.get("message", ""), groups.get("code"))
        return Diagnostic(
            raw=raw,
            message=message,
            severity=_severity(groups.get("severity")),
            source=source,
            path=path,
            line=_number(groups.get("line")),
            column=_number(groups.get("column")),
            code=code,
        )

    severity = _SEVERITY_RE.match(text)
    if severity:
        groups = severity.groupdict()
        code, message = _code_and_message(groups.get("body", ""), groups.get("code"))
        return Diagnostic(
            raw=raw,
            message=message,
            severity=_severity(groups.get("severity")),
            source=source,
            code=code,
        )
    return None


def parse_diagnostics(
    output: bytes | bytearray | memoryview | str,
    *,
    source: str = "compiler",
    max_bytes: int = 256 << 10,
    max_items: int = 256,
) -> tuple[Diagnostic, ...]:
    """Parse recognized diagnostics from bounded compiler/package output.

    Input is clipped before decoding and uses replacement decoding so a
    malformed tool byte cannot make the task view fail.  The original decoded
    line is kept in :attr:`Diagnostic.raw`; the complete exact byte stream
    remains owned by the build task itself.
    """

    if max_bytes < 1 or max_items < 1:
        raise ValueError("diagnostic limits must be positive")
    if isinstance(output, str):
        data = output.encode("utf-8", errors="strict")
    else:
        data = bytes(output)
    clipped = data[:max_bytes]
    text = clipped.decode("utf-8", errors="replace")
    parsed: list[Diagnostic] = []
    for raw in text.splitlines():
        diagnostic = _parse_line(raw, source)
        if diagnostic is not None:
            parsed.append(diagnostic)
            if len(parsed) >= max_items:
                break
    return tuple(parsed)


def diagnostics_for_lines(lines: Iterable[str], *, source: str = "compiler", max_items: int = 256) -> tuple[Diagnostic, ...]:
    """Parse already-split lines without bypassing the item bound."""

    if max_items < 1:
        raise ValueError("max_items must be positive")
    parsed: list[Diagnostic] = []
    for raw in lines:
        diagnostic = _parse_line(raw, source)
        if diagnostic is not None:
            parsed.append(diagnostic)
            if len(parsed) >= max_items:
                break
    return tuple(parsed)
