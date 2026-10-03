"""UTF-8-preserving source buffer with local undo and position conversion.

The visual form document has its own command history.  Source files need a
separate history and a byte-accurate coordinate contract for LSP, compiler,
and generated-source navigation.  This module deliberately stores the exact
loaded bytes and never normalizes line endings or a final newline.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class PositionError(ValueError):
    """A source position is outside the document or splits a UTF-8 unit."""


class ExternalState(Enum):
    CLEAN = "clean"
    CHANGED = "changed"
    MISSING = "missing"


@dataclass(frozen=True)
class SourceEdit:
    start_byte: int
    end_byte: int
    replacement: bytes

    @classmethod
    def text(cls, start_byte: int, end_byte: int, replacement: str) -> "SourceEdit":
        return cls(start_byte, end_byte, replacement.encode("utf-8"))


@dataclass(frozen=True)
class _HistoryEntry:
    before: bytes
    after: bytes


class SourceDocument:
    """An editable UTF-8 byte buffer with a monotonic source revision."""

    def __init__(self, data: bytes | bytearray | memoryview | str, *, max_history_bytes: int = 8 << 20):
        if max_history_bytes < 1:
            raise ValueError("max_history_bytes must be positive")
        self.max_history_bytes = max_history_bytes
        self._data = self._coerce_utf8(data)
        self._saved = self._data
        self._disk: bytes | None = self._data
        self._history: list[_HistoryEntry] = []
        self._history_cursor = 0
        self.revision = 1
        self._line_starts: list[int] = []
        self._reindex()

    @staticmethod
    def _coerce_utf8(data: bytes | bytearray | memoryview | str) -> bytes:
        if isinstance(data, str):
            return data.encode("utf-8")
        value = bytes(data)
        value.decode("utf-8", errors="strict")
        return value

    @property
    def data(self) -> bytes:
        return self._data

    @property
    def text(self) -> str:
        return self._data.decode("utf-8")

    @property
    def dirty(self) -> bool:
        return self._data != self._saved

    @property
    def external_state(self) -> ExternalState:
        if self._disk is None:
            return ExternalState.MISSING
        return ExternalState.CLEAN if self._disk == self._saved else ExternalState.CHANGED

    @property
    def line_count(self) -> int:
        return len(self._line_starts)

    @property
    def can_undo(self) -> bool:
        return self._history_cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._history_cursor < len(self._history)

    def _reindex(self) -> None:
        self._line_starts = [0]
        for index, value in enumerate(self._data):
            if value == 0x0A and index + 1 <= len(self._data):
                self._line_starts.append(index + 1)

    def _is_boundary(self, offset: int) -> bool:
        if offset <= 0 or offset >= len(self._data):
            return 0 <= offset <= len(self._data)
        return (self._data[offset] & 0xC0) != 0x80

    def _validate_edit(self, edit: SourceEdit) -> bytes:
        if edit.start_byte < 0 or edit.end_byte < edit.start_byte or edit.end_byte > len(self._data):
            raise PositionError("edit byte range is outside the source document")
        if not self._is_boundary(edit.start_byte) or not self._is_boundary(edit.end_byte):
            raise PositionError("edit byte range splits a UTF-8 code point")
        replacement = self._coerce_utf8(edit.replacement)
        return replacement

    def apply_edit(self, edit: SourceEdit) -> int:
        replacement = self._validate_edit(edit)
        before = self._data
        after = before[: edit.start_byte] + replacement + before[edit.end_byte :]
        if after == before:
            return self.revision
        if self._history_cursor < len(self._history):
            self._history = self._history[: self._history_cursor]
        self._history.append(_HistoryEntry(before, after))
        self._history_cursor += 1
        self._data = after
        self.revision += 1
        self._reindex()
        self._prune_history()
        return self.revision

    def _prune_history(self) -> None:
        total = sum(len(item.before) + len(item.after) for item in self._history)
        while self._history and total > self.max_history_bytes and self._history_cursor > 0:
            removed = self._history.pop(0)
            total -= len(removed.before) + len(removed.after)
            self._history_cursor -= 1

    def _restore(self, value: bytes) -> int:
        self._data = value
        self.revision += 1
        self._reindex()
        return self.revision

    def undo(self) -> bool:
        if not self.can_undo:
            return False
        self._history_cursor -= 1
        self._restore(self._history[self._history_cursor].before)
        return True

    def redo(self) -> bool:
        if not self.can_redo:
            return False
        self._restore(self._history[self._history_cursor].after)
        self._history_cursor += 1
        return True

    def mark_saved(self, disk_bytes: bytes | bytearray | memoryview | str | None = None) -> None:
        if disk_bytes is None:
            disk = self._data
        else:
            disk = self._coerce_utf8(disk_bytes)
        self._saved = self._data
        self._disk = disk

    def observe_disk(self, disk_bytes: bytes | bytearray | memoryview | str | None) -> ExternalState:
        self._disk = None if disk_bytes is None else self._coerce_utf8(disk_bytes)
        return self.external_state

    def reload_from_disk(self, disk_bytes: bytes | bytearray | memoryview | str) -> int:
        if self.dirty:
            raise PositionError("cannot reload a dirty source document")
        value = self._coerce_utf8(disk_bytes)
        self._data = value
        self._saved = value
        self._disk = value
        self._history.clear()
        self._history_cursor = 0
        self.revision += 1
        self._reindex()
        return self.revision

    def line_start_byte(self, line: int) -> int:
        if line < 0 or line >= len(self._line_starts):
            raise PositionError("line is outside the source document")
        return self._line_starts[line]

    def _line_content_end(self, line: int) -> int:
        start = self.line_start_byte(line)
        end = self._line_starts[line + 1] if line + 1 < len(self._line_starts) else len(self._data)
        if end > start and self._data[end - 1] == 0x0A:
            end -= 1
        if end > start and self._data[end - 1] == 0x0D:
            end -= 1
        return end

    def line_byte_range(self, line: int, *, include_ending: bool = False) -> tuple[int, int]:
        """Return one line's byte range, excluding CRLF/LF unless requested."""
        start = self.line_start_byte(line)
        if include_ending and line + 1 < len(self._line_starts):
            return start, self._line_starts[line + 1]
        return start, self._line_content_end(line)

    def find_all(
        self,
        query: str,
        *,
        start_byte: int = 0,
        max_matches: int = 10_000,
    ) -> tuple[tuple[int, int], ...]:
        """Find literal UTF-8 text and return exact byte ranges.

        Matching is case-sensitive and non-overlapping. Empty queries and
        invalid UTF-8 boundaries are rejected rather than assigned ambiguous
        caret locations. The result count is bounded for large source files.
        """
        if not isinstance(query, str) or not query:
            raise ValueError("search text must be nonempty text")
        if type(max_matches) is not int or max_matches < 1:
            raise ValueError("search match limit must be a positive integer")
        if type(start_byte) is not int or not self._is_boundary(start_byte):
            raise PositionError("search start must be a UTF-8 byte boundary")
        needle = query.encode("utf-8", errors="strict")
        matches: list[tuple[int, int]] = []
        cursor = start_byte
        while cursor <= len(self._data):
            found = self._data.find(needle, cursor)
            if found < 0:
                break
            finish = found + len(needle)
            if self._is_boundary(found) and self._is_boundary(finish):
                matches.append((found, finish))
                if len(matches) > max_matches:
                    raise ValueError("search match limit exceeded")
            cursor = finish if finish > cursor else cursor + 1
        return tuple(matches)

    def replace_all(self, query: str, replacement: str, *, max_matches: int = 10_000) -> int:
        """Replace all literal matches as one source-history transaction."""
        if not isinstance(replacement, str):
            raise TypeError("replacement must be text")
        ranges = self.find_all(query, max_matches=max_matches)
        if not ranges:
            return 0
        inserted = replacement.encode("utf-8", errors="strict")
        output = bytearray()
        cursor = 0
        for start, end in ranges:
            output.extend(self._data[cursor:start])
            output.extend(inserted)
            cursor = end
        output.extend(self._data[cursor:])
        self.apply_edit(SourceEdit(0, len(self._data), bytes(output)))
        return len(ranges)

    def indent_lines(
        self,
        first_line: int,
        last_line: int,
        indentation: str = "    ",
    ) -> bool:
        """Prefix an inclusive line range in one undoable edit."""
        self._validate_line_range(first_line, last_line)
        prefix = self._indent_bytes(indentation)
        if not prefix:
            return False
        output = bytearray()
        cursor = 0
        for line in range(first_line, last_line + 1):
            start = self.line_start_byte(line)
            output.extend(self._data[cursor:start])
            output.extend(prefix)
            cursor = start
        output.extend(self._data[cursor:])
        self.apply_edit(SourceEdit(0, len(self._data), bytes(output)))
        return True

    def outdent_lines(
        self,
        first_line: int,
        last_line: int,
        indentation: str = "    ",
    ) -> bool:
        """Remove one configured indentation unit, or one leading tab."""
        self._validate_line_range(first_line, last_line)
        prefix = self._indent_bytes(indentation)
        if not prefix:
            return False
        removals: list[tuple[int, int]] = []
        for line in range(first_line, last_line + 1):
            start = self.line_start_byte(line)
            if self._data.startswith(prefix, start):
                removals.append((start, start + len(prefix)))
            elif self._data[start:start + 1] == b"\t":
                removals.append((start, start + 1))
            elif prefix == b" " * len(prefix):
                count = 0
                while count < len(prefix) and self._data[start + count:start + count + 1] == b" ":
                    count += 1
                if count:
                    removals.append((start, start + count))
        if not removals:
            return False
        output = bytearray()
        cursor = 0
        for start, end in removals:
            output.extend(self._data[cursor:start])
            cursor = end
        output.extend(self._data[cursor:])
        self.apply_edit(SourceEdit(0, len(self._data), bytes(output)))
        return True

    @staticmethod
    def _indent_bytes(indentation: str) -> bytes:
        if not isinstance(indentation, str):
            raise TypeError("indentation must be text")
        encoded = indentation.encode("utf-8", errors="strict")
        if any(byte not in (0x20, 0x09) for byte in encoded):
            raise ValueError("indentation may contain only spaces and tabs")
        return encoded

    def _validate_line_range(self, first_line: int, last_line: int) -> None:
        if (
            type(first_line) is not int
            or type(last_line) is not int
            or first_line < 0
            or last_line < first_line
            or last_line >= self.line_count
        ):
            raise PositionError("line range is outside the source document")

    @staticmethod
    def _utf16_units(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    def byte_to_position(self, offset: int, *, encoding: str = "utf-8") -> tuple[int, int]:
        if encoding not in ("utf-8", "utf-16", "utf-32"):
            raise ValueError("encoding must be utf-8, utf-16, or utf-32")
        if offset < 0 or offset > len(self._data) or not self._is_boundary(offset):
            raise PositionError("byte offset is outside the source or splits UTF-8")
        line = bisect.bisect_right(self._line_starts, offset) - 1
        start = self._line_starts[line]
        end = min(offset, self._line_content_end(line))
        prefix = self._data[start:end].decode("utf-8")
        if encoding == "utf-8":
            character = len(self._data[start:end])
        elif encoding == "utf-16":
            character = self._utf16_units(prefix)
        else:
            # Python iterates Unicode scalar values, which is the unit LSP
            # calls UTF-32. UTF-8 continuation bytes and UTF-16 surrogate
            # halves are never exposed as positions by this branch.
            character = len(prefix)
        return line, character

    def position_to_byte(self, line: int, character: int, *, encoding: str = "utf-8") -> int:
        if encoding not in ("utf-8", "utf-16", "utf-32"):
            raise ValueError("encoding must be utf-8, utf-16, or utf-32")
        if character < 0:
            raise PositionError("character must be nonnegative")
        start = self.line_start_byte(line)
        end = self._line_content_end(line)
        content = self._data[start:end]
        if encoding == "utf-8":
            if character > len(content):
                raise PositionError("UTF-8 character is past the line")
            if not self._is_boundary(start + character):
                raise PositionError("UTF-8 character splits a code point")
            return start + character
        text = content.decode("utf-8")
        units = 0
        byte_offset = 0
        for scalar in text:
            width = self._utf16_units(scalar) if encoding == "utf-16" else 1
            if units == character:
                return start + byte_offset
            if units < character < units + width:
                raise PositionError("UTF-16 character splits a surrogate pair")
            units += width
            byte_offset += len(scalar.encode("utf-8"))
        if units == character:
            return start + byte_offset
        raise PositionError(f"{encoding.upper()} character is past the line")
