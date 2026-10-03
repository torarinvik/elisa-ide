#!/usr/bin/env python3
"""Unicode, line-ending, history, and external-file source-buffer contracts."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from source import ExternalState, PositionError, SourceDocument, SourceEdit  # noqa: E402


def test_positions_and_exact_bytes() -> None:
    original = "first\r\nemoji 😀 + combining e\u0301\r\nlast"
    document = SourceDocument(original)
    assert document.data == original.encode("utf-8")
    assert document.line_count == 3
    emoji = document.text.index("😀")
    emoji_byte = len(document.text[:emoji].encode("utf-8"))
    assert document.byte_to_position(emoji_byte, encoding="utf-8") == (1, len("emoji ".encode()))
    assert document.byte_to_position(emoji_byte, encoding="utf-16") == (1, 6)
    assert document.byte_to_position(emoji_byte, encoding="utf-32") == (1, 6)
    assert document.position_to_byte(1, 6, encoding="utf-16") == emoji_byte
    assert document.position_to_byte(1, 6, encoding="utf-32") == emoji_byte
    assert document.position_to_byte(1, 8, encoding="utf-16") == emoji_byte + len("😀".encode())
    assert document.position_to_byte(1, 7, encoding="utf-32") == emoji_byte + len("😀".encode())
    try:
        document.position_to_byte(1, 7, encoding="utf-16")
    except PositionError:
        pass
    else:
        raise AssertionError("UTF-16 surrogate split was accepted")
    # CRLF bytes remain untouched; line positions stop before the CRLF pair.
    assert document.position_to_byte(0, 5) == len(b"first")
    assert document.byte_to_position(len(b"first\r")) == (0, 5)


def test_edit_history_and_external_state() -> None:
    document = SourceDocument("one\ntwo\n")
    assert not document.dirty and document.external_state is ExternalState.CLEAN
    document.apply_edit(SourceEdit.text(0, 3, "uno"))
    assert document.text == "uno\ntwo\n" and document.dirty and document.can_undo
    assert document.revision == 2
    assert document.undo() and document.text == "one\ntwo\n" and not document.dirty
    assert document.redo() and document.text == "uno\ntwo\n" and document.dirty
    document.observe_disk("one\ntwo\n")
    assert document.external_state is ExternalState.CLEAN
    document.observe_disk("disk\ntwo\n")
    assert document.external_state is ExternalState.CHANGED
    try:
        document.reload_from_disk("disk\ntwo\n")
    except PositionError:
        pass
    else:
        raise AssertionError("dirty source was reloaded")
    assert document.data == b"uno\ntwo\n"
    document.undo()
    document.reload_from_disk("disk\ntwo\n")
    assert document.text == "disk\ntwo\n" and not document.dirty
    document.observe_disk(None)
    assert document.external_state is ExternalState.MISSING


def test_invalid_utf8_boundaries_and_branching() -> None:
    try:
        SourceDocument(b"bad\xff")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("invalid UTF-8 source was accepted")
    document = SourceDocument("ab😀cd", max_history_bytes=1024)
    emoji_start = len("ab".encode())
    try:
        document.apply_edit(SourceEdit(emoji_start + 1, emoji_start + 1, b"x"))
    except PositionError:
        pass
    else:
        raise AssertionError("edit split a UTF-8 code point")
    document.apply_edit(SourceEdit.text(0, 2, "AB"))
    document.undo()
    document.apply_edit(SourceEdit.text(0, 2, "XY"))
    assert not document.can_redo and document.text == "XY😀cd"


def test_search_replace_indent_and_line_ranges() -> None:
    original = "head\r\nold = \"😀\"\r\n    old_value\n"
    document = SourceDocument(original)
    emoji_start = len("head\r\nold = \"".encode("utf-8"))
    assert document.line_byte_range(0) == (0, 4)
    assert document.line_byte_range(0, include_ending=True) == (0, 6)
    assert document.find_all("😀") == ((emoji_start, emoji_start + 4),)
    assert document.find_all("old") == (
        (len("head\r\n".encode()), len("head\r\nold".encode())),
        (len("head\r\nold = \"😀\"\r\n    ".encode()),
         len("head\r\nold = \"😀\"\r\n    old".encode())),
    )
    assert document.replace_all("old", "new") == 2
    assert document.text == "head\r\nnew = \"😀\"\r\n    new_value\n"
    replaced = document.data
    assert document.replace_all("missing", "unused") == 0
    assert document.data == replaced
    assert document.undo() and document.text == original
    assert document.indent_lines(1, 2, "\t")
    assert document.text == "head\r\n\told = \"😀\"\r\n\t    old_value\n"
    assert document.outdent_lines(1, 2, "\t") and document.text == original
    assert document.undo() and document.text == "head\r\n\told = \"😀\"\r\n\t    old_value\n"

    for call in (
        lambda: document.find_all("", max_matches=1),
        lambda: document.find_all("x", max_matches=True),
        lambda: document.find_all("x", start_byte=emoji_start + 2),
        lambda: document.indent_lines(3, 4),
        lambda: document.indent_lines(0, 0, "  \tX"),
    ):
        try:
            call()
        except (ValueError, PositionError):
            pass
        else:
            raise AssertionError("invalid source editing request was accepted")

    limited = SourceDocument("a a a")
    try:
        limited.find_all("a", max_matches=2)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("search match limit was not enforced")
    assert limited.text == "a a a" and not limited.can_undo


def main() -> int:
    test_positions_and_exact_bytes()
    test_edit_history_and_external_state()
    test_invalid_utf8_boundaries_and_branching()
    test_search_replace_indent_and_line_ranges()
    print("source document: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
