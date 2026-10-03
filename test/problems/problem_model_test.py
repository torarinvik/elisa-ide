#!/usr/bin/env python3
"""Contract tests for the shared, bounded Problems projection."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from build.build_diagnostics import parse_diagnostics  # noqa: E402
from lsp.lsp_client import PublishedDiagnostic  # noqa: E402
from problems import Problem, ProblemModel, ProblemSeverity, ProblemSource  # noqa: E402
from source import SourceDocument  # noqa: E402


def test_lsp_coordinate_mapping_and_producer_adapters() -> None:
    uri = "file:///workspace/source.elisa"
    document = SourceDocument("let name = \"😀\"\n")
    raw = {
        "message": "bad value",
        "severity": 1,
        "source": "Elisa-LSP",
        "code": "E42",
        "range": {
            "start": {"line": 0, "character": 12},
            "end": {"line": 0, "character": 14},
        },
        "relatedInformation": [{
            "location": {
                "uri": "file:///workspace/declaration.elisa",
                "range": {"start": {"line": 4, "character": 2}, "end": {"line": 4, "character": 9}},
            },
            "message": "declared here",
        }],
    }
    diagnostic = PublishedDiagnostic.from_mapping(uri, 8, raw)
    assert diagnostic is not None
    problem = Problem.from_lsp(diagnostic, document=document, encoding="utf-16", revision=12)
    assert problem.severity is ProblemSeverity.ERROR
    assert problem.producer == "Elisa-LSP"
    assert problem.uri == uri and problem.revision == 12
    assert problem.position_encoding == "utf-16"
    assert (problem.start_byte, problem.end_byte) == (12, 16)
    related = problem.related_information[0]
    assert (related.message, related.uri) == ("declared here", "file:///workspace/declaration.elisa")
    assert (related.start_line, related.start_character, related.end_line, related.end_character) == (4, 2, 4, 9)

    build_problem = Problem.from_build(
        parse_diagnostics("src/main.elisa:4:9: error[E17]: missing value")[0],
        revision=12,
        task_id="build-21",
    )
    assert build_problem.source == ProblemSource.COMPILER.value
    assert (build_problem.start_line, build_problem.start_character) == (3, 8)
    assert build_problem.position_encoding == "compiler-1-based-normalized"
    assert build_problem.task_id == "build-21" and build_problem.code == "E17"
    package_problem = Problem.from_build(
        parse_diagnostics("error: missing local package", source="elisapkg")[0]
    )
    assert package_problem.source == ProblemSource.PACKAGE.value
    assert package_problem.producer == "elisapkg"

    design_problem = Problem.from_design(
        {"code": "missing-handler", "severity": 2, "message": "Connect the event"},
        path="forms/main.elisaform.json",
    )
    assert design_problem.source == ProblemSource.DESIGN.value
    assert design_problem.severity is ProblemSeverity.WARNING
    assert design_problem.path == "forms/main.elisaform.json"


def test_revision_replacement_bounds_and_stable_rows() -> None:
    def item(message: str) -> Problem:
        return Problem(
            problem_id="same-source-span",
            source=ProblemSource.COMPILER.value,
            severity=ProblemSeverity.ERROR,
            message=message,
        )

    model = ProblemModel(max_items=3, max_groups=4)
    assert model.replace_group("compiler", "active-build", [item("first")], revision=4, task_id="a")
    assert not model.replace_group("compiler", "active-build", [item("stale")], revision=3, task_id="old")
    assert model.problems[0].message == "first"
    assert model.replace_group("compiler", "active-build", [item("current"), item("duplicate")], revision=5, task_id="b")
    assert [problem.problem_id for problem in model.problems] == [
        "same-source-span",
        "same-source-span:2",
    ]
    assert model.group_revision("compiler", "active-build") == 5
    assert not model.clear_group("compiler", "active-build", revision=4)
    assert model.clear_group("compiler", "active-build", revision=5)
    assert model.count == 0
    assert not model.replace_group("compiler", "active-build", [item("late stale")], revision=4)

    item_limited = ProblemModel(max_items=3)
    assert item_limited.replace_group("compiler", "one", [item("a"), item("b")], revision=1)
    try:
        item_limited.replace_group("compiler", "two", [item("c"), item("d")], revision=1)
    except ValueError as exc:
        assert "item limit" in str(exc)
    else:
        raise AssertionError("problem item limit was not enforced")


def test_invalid_coordinates_and_limits() -> None:
    try:
        Problem(
            problem_id="bad-range",
            source="lsp",
            severity=ProblemSeverity.ERROR,
            message="bad",
            start_byte=9,
            end_byte=8,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("reversed problem byte range was accepted")

    try:
        ProblemModel(max_items=True)
    except ValueError:
        pass
    else:
        raise AssertionError("boolean item limit was accepted")


def main() -> int:
    test_lsp_coordinate_mapping_and_producer_adapters()
    test_revision_replacement_bounds_and_stable_rows()
    test_invalid_coordinates_and_limits()
    print("problem model: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
