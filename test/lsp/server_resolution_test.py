from __future__ import annotations

import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.lsp.server_resolution import LspResolutionError, resolve_server


def executable(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    path.chmod(0o755)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "workspace"
        root.mkdir()
        workspace = root / "configured-lsp"
        nearby = root / "build" / "elisa-lsp"
        env_path = root / "env-lsp"
        path_path = root / "path-lsp"
        executable(workspace, b"workspace")
        executable(nearby, b"nearby")
        executable(env_path, b"environment")
        executable(path_path, b"path")

        selected = resolve_server(
            root,
            workspace_setting=workspace,
            trusted_nearby=nearby,
            env={"ELISA_LSP": str(env_path)},
            path_lookup=lambda _: str(path_path),
        )
        assert selected.path == workspace.resolve()
        assert selected.selected_by == "workspace setting"
        assert selected.size == len(b"workspace")

        selected = resolve_server(
            root,
            trusted_nearby=nearby,
            env={"ELISA_LSP": str(env_path)},
            path_lookup=lambda _: str(path_path),
        )
        assert selected.path == nearby.resolve()
        assert selected.selected_by == "trusted nearby build"

        selected = resolve_server(root, env={"ELISA_LSP": str(env_path)}, path_lookup=lambda _: str(path_path))
        assert selected.path == env_path.resolve()
        assert selected.selected_by == "ELISA_LSP"

        selected = resolve_server(root, env={}, path_lookup=lambda _: str(path_path))
        assert selected.path == path_path.resolve()
        assert selected.selected_by == "PATH"

        try:
            resolve_server(root, workspace_setting=root / "missing")
        except LspResolutionError as exc:
            assert "workspace setting" in str(exc)
        else:
            raise AssertionError("invalid workspace setting silently fell through")

        try:
            resolve_server(root, env={}, path_lookup=lambda _: None)
        except LspResolutionError:
            pass
        else:
            raise AssertionError("missing LSP was accepted")
    print("ok lsp server resolution")


if __name__ == "__main__":
    main()
