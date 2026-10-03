#!/usr/bin/env python3
"""Compiler/package diagnostic parsing and bounded raw-line contracts."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from build import DiagnosticSeverity, parse_diagnostics  # noqa: E402


def main() -> int:
    output = (
        "generated/Grüße view.elisa:12:7: error[E2001]: unknown symbol\n"
        "forms/main.elisaform.json(4,2): warning[layout]: clipped child\n"
        "error: registry dependencies are not supported locally\n"
        "built project application: build/elisa_project_app\n"
        "note: use a local package graph\n"
    )
    diagnostics = parse_diagnostics(output, source="elisapkg")
    assert len(diagnostics) == 4
    first, second, third, fourth = diagnostics
    assert first.path == "generated/Grüße view.elisa"
    assert first.line == 12 and first.column == 7
    assert first.severity is DiagnosticSeverity.ERROR and first.code == "E2001"
    assert first.message == "unknown symbol" and first.raw.endswith("unknown symbol")
    assert second.path == "forms/main.elisaform.json"
    assert second.line == 4 and second.column == 2
    assert second.severity is DiagnosticSeverity.WARNING and second.code == "layout"
    assert third.path is None and not third.has_location
    assert third.severity is DiagnosticSeverity.ERROR
    assert fourth.severity is DiagnosticSeverity.NOTE
    assert all(item.source == "elisapkg" for item in diagnostics)

    clipped = parse_diagnostics(b"x" * 4 + b"\nfile.elisa:1:1: error: too late\n", max_bytes=4)
    assert clipped == ()
    bounded = parse_diagnostics("\n".join(f"file.elisa:{i}: error: bad" for i in range(1, 8)), max_items=3)
    assert len(bounded) == 3 and bounded[-1].line == 3
    try:
        parse_diagnostics("x", max_bytes=0)
    except ValueError:
        pass
    else:
        raise AssertionError("zero diagnostic limit was accepted")
    print("build diagnostics: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
