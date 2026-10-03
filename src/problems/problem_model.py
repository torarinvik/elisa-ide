"""Bounded, revision-aware aggregation for the IDE Problems view.

Each producer keeps its native protocol details at its boundary. This module
provides a small shared projection for the UI and carries coordinate encoding
through instead of treating every tool's columns as interchangeable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from build.build_diagnostics import Diagnostic as BuildDiagnostic
from lsp.lsp_client import PublishedDiagnostic
from source import SourceDocument


class ProblemSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


class ProblemSource(str, Enum):
    LSP = "lsp"
    COMPILER = "compiler"
    PACKAGE = "package"
    DESIGN = "design"
    GENERATION = "generation"
    PREVIEW = "preview"
    TEST = "test"
    DEBUG = "debug"
    PROFILE = "profile"


_MAX_FIELD_CHARS = 8192


@dataclass(frozen=True)
class RelatedInformation:
    """One LSP related-information note with its own source location."""

    message: str
    uri: str | None = None
    start_line: int | None = None
    start_character: int | None = None
    end_line: int | None = None
    end_character: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, str):
            raise TypeError("related information message must be text")
        object.__setattr__(self, "message", self.message[:_MAX_FIELD_CHARS])
        if self.uri is not None:
            if not isinstance(self.uri, str):
                raise TypeError("related information URI must be text")
            object.__setattr__(self, "uri", self.uri[:_MAX_FIELD_CHARS])
        for name in ("start_line", "start_character", "end_line", "end_character"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"related information {name} must be nonnegative")


def _severity(value: Any) -> ProblemSeverity:
    if isinstance(value, ProblemSeverity):
        return value
    if isinstance(value, int) and type(value) is int:
        return {
            1: ProblemSeverity.ERROR,
            2: ProblemSeverity.WARNING,
            3: ProblemSeverity.INFO,
            4: ProblemSeverity.HINT,
        }.get(value, ProblemSeverity.INFO)
    normalized = str(value).lower()
    try:
        return ProblemSeverity(normalized)
    except ValueError:
        return ProblemSeverity.INFO


def _code(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) not in (str, int):
        return None
    text = str(value)
    return text[:256] if text else None


def _stable_id(parts: Iterable[Any]) -> str:
    digest = hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return digest[:24]


@dataclass(frozen=True)
class Problem:
    """One normalized, UI-safe diagnostic with explicit source coordinates."""

    problem_id: str
    source: str
    severity: ProblemSeverity
    message: str
    producer: str | None = None
    code: str | None = None
    path: str | None = None
    uri: str | None = None
    start_line: int | None = None
    start_character: int | None = None
    end_line: int | None = None
    end_character: int | None = None
    position_encoding: str | None = None
    start_byte: int | None = None
    end_byte: int | None = None
    revision: int | None = None
    task_id: str | None = None
    related_information: tuple[RelatedInformation, ...] = ()

    def __post_init__(self) -> None:
        if not self.problem_id or not self.source:
            raise ValueError("problem ID and source must be nonempty")
        if not isinstance(self.severity, ProblemSeverity):
            raise TypeError("problem severity must be a ProblemSeverity")
        if not isinstance(self.message, str):
            raise TypeError("problem message must be text")
        for name in ("problem_id", "source", "message", "producer", "code", "path", "uri", "position_encoding", "task_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be text when provided")
            if isinstance(value, str):
                limit = 256 if name in ("problem_id", "source", "producer", "code", "position_encoding", "task_id") else _MAX_FIELD_CHARS
                object.__setattr__(self, name, value[:limit])
        for name in ("start_line", "start_character", "end_line", "end_character", "start_byte", "end_byte", "revision"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer when provided")
        if self.end_byte is not None and self.start_byte is None:
            raise ValueError("an end byte requires a start byte")
        if self.start_byte is not None and self.end_byte is not None and self.end_byte < self.start_byte:
            raise ValueError("problem byte range is reversed")
        if not isinstance(self.related_information, tuple) or any(
            not isinstance(item, RelatedInformation) for item in self.related_information
        ):
            raise TypeError("related information must be a tuple of RelatedInformation")

    @classmethod
    def from_lsp(
        cls,
        diagnostic: PublishedDiagnostic,
        *,
        document: SourceDocument | None = None,
        encoding: str | None = None,
        revision: int | None = None,
    ) -> "Problem":
        byte_range = (
            diagnostic.byte_range(document, encoding=encoding)
            if document is not None and encoding is not None
            else None
        )
        related: list[RelatedInformation] = []
        for item in diagnostic.related_information[:32]:
            location = item.get("location")
            if not isinstance(location, Mapping):
                location = {}
            uri = location.get("uri") if isinstance(location.get("uri"), str) else None
            source_range = location.get("range")
            if not isinstance(source_range, Mapping):
                source_range = {}
            start = source_range.get("start")
            end = source_range.get("end")
            if not isinstance(start, Mapping):
                start = {}
            if not isinstance(end, Mapping):
                end = {}
            message = item.get("message", "")
            if not isinstance(message, str):
                message = ""
            related.append(RelatedInformation(
                message=message,
                uri=uri,
                start_line=start.get("line") if type(start.get("line")) is int and start["line"] >= 0 else None,
                start_character=start.get("character") if type(start.get("character")) is int and start["character"] >= 0 else None,
                end_line=end.get("line") if type(end.get("line")) is int and end["line"] >= 0 else None,
                end_character=end.get("character") if type(end.get("character")) is int and end["character"] >= 0 else None,
            ))
        return cls(
            problem_id=_stable_id(("lsp", diagnostic.uri, diagnostic.version, diagnostic.code,
                                   diagnostic.start_line, diagnostic.start_character, diagnostic.message)),
            source=ProblemSource.LSP.value,
            severity=_severity(diagnostic.severity),
            message=diagnostic.message,
            producer=diagnostic.source,
            code=_code(diagnostic.code),
            uri=diagnostic.uri,
            start_line=diagnostic.start_line,
            start_character=diagnostic.start_character,
            end_line=diagnostic.end_line,
            end_character=diagnostic.end_character,
            position_encoding=encoding,
            start_byte=byte_range[0] if byte_range is not None else None,
            end_byte=byte_range[1] if byte_range is not None else None,
            revision=revision,
            related_information=tuple(related),
        )

    @classmethod
    def from_build(
        cls,
        diagnostic: BuildDiagnostic,
        *,
        revision: int | None = None,
        task_id: str | None = None,
    ) -> "Problem":
        producer = diagnostic.source
        normalized_producer = producer.lower()
        source = (
            ProblemSource.PACKAGE.value
            if "package" in normalized_producer or "pkg" in normalized_producer
            else ProblemSource.COMPILER.value
        )
        line = diagnostic.line - 1 if diagnostic.line is not None and diagnostic.line > 0 else None
        column = diagnostic.column - 1 if diagnostic.column is not None and diagnostic.column > 0 else None
        return cls(
            problem_id=_stable_id((source, task_id, diagnostic.path, diagnostic.line, diagnostic.column,
                                   diagnostic.code, diagnostic.message)),
            source=source,
            severity=_severity(diagnostic.severity.value),
            message=diagnostic.message,
            producer=producer,
            code=_code(diagnostic.code),
            path=diagnostic.path,
            start_line=line,
            start_character=column,
            position_encoding="compiler-1-based-normalized",
            revision=revision,
            task_id=task_id,
        )

    @classmethod
    def from_design(
        cls,
        diagnostic: Mapping[str, Any],
        *,
        path: str | None = None,
        revision: int | None = None,
    ) -> "Problem":
        message = diagnostic.get("message", "")
        if not isinstance(message, str):
            raise ValueError("design diagnostic message must be text")
        source_path = diagnostic.get("path")
        if not isinstance(source_path, str):
            source_path = path
        code = _code(diagnostic.get("code"))
        return cls(
            problem_id=_stable_id(("design", source_path, code, message)),
            source=ProblemSource.DESIGN.value,
            severity=_severity(diagnostic.get("severity")),
            message=message,
            producer=ProblemSource.DESIGN.value,
            code=code,
            path=source_path,
            revision=revision,
        )


@dataclass(frozen=True)
class _ProblemGroup:
    revision: int
    task_id: str | None
    items: tuple[Problem, ...]


class ProblemModel:
    """Replaceable diagnostic groups with bounds and stale-revision rejection."""

    def __init__(self, *, max_items: int = 2048, max_groups: int = 4096):
        if type(max_items) is not int or max_items < 1 or type(max_groups) is not int or max_groups < 1:
            raise ValueError("problem limits must be positive integers")
        self.max_items = max_items
        self.max_groups = max_groups
        self._groups: dict[tuple[str, str], _ProblemGroup] = {}

    @property
    def problems(self) -> tuple[Problem, ...]:
        return tuple(item for group in self._groups.values() for item in group.items)

    @property
    def count(self) -> int:
        return sum(len(group.items) for group in self._groups.values())

    def replace_group(
        self,
        source: str | ProblemSource,
        owner: str,
        problems: Iterable[Problem],
        *,
        revision: int,
        task_id: str | None = None,
    ) -> bool:
        source_name = source.value if isinstance(source, ProblemSource) else source
        if not source_name or not owner:
            raise ValueError("problem source and group owner must be nonempty")
        if type(revision) is not int or revision < 0:
            raise ValueError("problem group revision must be a nonnegative integer")
        incoming_list: list[Problem] = []
        for item in problems:
            if len(incoming_list) >= self.max_items:
                raise ValueError("problem item limit exceeded")
            incoming_list.append(item)
        incoming = tuple(incoming_list)
        if any(not isinstance(item, Problem) or item.source != source_name for item in incoming):
            raise ValueError("every problem must match the group source")
        key = (source_name, owner)
        previous = self._groups.get(key)
        if previous is not None and revision < previous.revision:
            return False
        if previous is None and len(self._groups) >= self.max_groups:
            raise ValueError("problem group limit exceeded")
        resulting_count = self.count - (len(previous.items) if previous is not None else 0) + len(incoming)
        if resulting_count > self.max_items:
            raise ValueError("problem item limit exceeded")
        unique: list[Problem] = []
        seen: dict[str, int] = {}
        for item in incoming:
            occurrence = seen.get(item.problem_id, 0) + 1
            seen[item.problem_id] = occurrence
            unique.append(item if occurrence == 1 else replace(item, problem_id=f"{item.problem_id}:{occurrence}"))
        self._groups[key] = _ProblemGroup(revision, task_id, tuple(unique))
        return True

    def clear_group(self, source: str | ProblemSource, owner: str, *, revision: int) -> bool:
        source_name = source.value if isinstance(source, ProblemSource) else source
        if type(revision) is not int or revision < 0:
            raise ValueError("problem group revision must be a nonnegative integer")
        key = (source_name, owner)
        previous = self._groups.get(key)
        if previous is None or revision < previous.revision:
            return False
        # Retain an empty revision tombstone so an older async result cannot
        # repopulate a group after a newer edit cleared it.
        self._groups[key] = _ProblemGroup(revision, previous.task_id, ())
        return True

    def group_revision(self, source: str | ProblemSource, owner: str) -> int | None:
        source_name = source.value if isinstance(source, ProblemSource) else source
        group = self._groups.get((source_name, owner))
        return group.revision if group is not None else None
