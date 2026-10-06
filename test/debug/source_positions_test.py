#!/usr/bin/env python3
"""EDIR UTF-8 byte and DAP UTF-16 coordinate contracts."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from debug.source_positions import dap_to_edir_position, edir_to_dap_position  # noqa: E402
from source import PositionError, SourceDocument  # noqa: E402


def main() -> int:
    document = SourceDocument("a😀e\u0301\r\n𝄞x\n")
    expected = {
        (1, 1): (1, 0),
        (1, 2): (1, 1),
        (1, 6): (1, 3),
        (1, 7): (1, 4),
        (1, 9): (1, 5),
        (2, 1): (2, 0),
        (2, 5): (2, 2),
        (2, 6): (2, 3),
    }
    for edir, dap in expected.items():
        assert edir_to_dap_position(document, *edir) == dap, edir
        assert dap_to_edir_position(document, *dap) == edir, (edir, dap)

    for invalid in ((0, 1), (1, 0), (True, 1), (1, True), (4, 1), (1, 10)):
        try:
            edir_to_dap_position(document, *invalid)
        except PositionError:
            pass
        else:
            raise AssertionError(f"invalid EDIR coordinate was accepted: {invalid!r}")

    try:
        dap_to_edir_position(document, 1, 2)
    except PositionError as exc:
        assert "surrogate" in str(exc).lower()
    else:
        raise AssertionError("DAP UTF-16 coordinate split an emoji surrogate pair")
    try:
        dap_to_edir_position(document, 1, 6)
    except PositionError:
        pass
    else:
        raise AssertionError("DAP column past the line was accepted")

    print("debug source positions: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
