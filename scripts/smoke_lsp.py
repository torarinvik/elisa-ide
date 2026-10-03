#!/usr/bin/env python3
"""Exercise the real local Elisa-LSP through the IDE transport boundary."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lsp import LspError, LspSupervisor, path_to_uri, resolve_server as select_server  # noqa: E402
from lsp.workspace_service import LspWorkspace  # noqa: E402
from problems import ProblemModel  # noqa: E402
from source import SourceEdit  # noqa: E402


def resolve_server(explicit: str | None) -> str:
    try:
        selected = select_server(
            ROOT,
            workspace_setting=explicit,
            trusted_nearby=(ROOT.parent / "Elisa-LSP" / "build" / "elisa-lsp")
            if explicit is None and (ROOT.parent / "Elisa-LSP" / "build" / "elisa-lsp").is_file()
            else None,
        )
    except Exception as exc:
        raise LspError(str(exc)) from exc
    return str(selected.path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server")
    args = parser.parse_args()
    server = resolve_server(args.server)
    supervisor = LspSupervisor(
        [server],
        cwd=ROOT,
        initialize_params={
            "processId": os.getpid(),
            "rootUri": path_to_uri(ROOT),
            "workspaceFolders": [{"uri": path_to_uri(ROOT), "name": "Elisa IDE"}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-8", "utf-16"]},
            },
        },
        client_options={"startup_timeout": 5.0, "request_timeout": 5.0},
    )
    workspace = LspWorkspace(supervisor, ProblemModel())
    uri = path_to_uri(ROOT / "build" / "lsp-smoke-π.elisa")
    try:
        workspace.start()
        buffer = workspace.open_document(uri, "def main() -> i64:\n    return 0\n")
        if buffer.sync_error:
            raise LspError(f"could not open source buffer: {buffer.sync_error}")
        first = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=5.0)
        if first.get("params", {}).get("version") != buffer.lsp_version:
            raise LspError(f"LSP diagnostics version mismatch: {first!r}")
        workspace.refresh_problems(uri)

        prefix = b"def main() -> i64:\n    return "
        workspace.apply_edit(uri, SourceEdit(len(prefix), len(prefix) + 1, b"???"))
        # Providers may publish an intermediate clear followed by diagnostics;
        # consume notifications until the active source revision is observed.
        current = None
        for _ in range(4):
            note = supervisor.wait_notification("textDocument/publishDiagnostics", timeout=5.0)
            if note.get("params", {}).get("version") == buffer.lsp_version:
                current = note
                break
        if current is None:
            raise LspError(f"real Elisa-LSP did not publish diagnostics for version {buffer.lsp_version}")
        workspace.refresh_problems(uri)
        if not workspace.problems.problems:
            raise LspError("versioned diagnostics were not projected into the shared Problems model")
        workspace.close_document(uri)
        supervisor.close()
    finally:
        supervisor.abort()
    print(f"lsp smoke: passed ({server})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LspError as exc:
        print(f"lsp smoke: FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
