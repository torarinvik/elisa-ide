from __future__ import annotations

import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.toolchain.resolver import ToolchainError, resolve_tool  # noqa: E402


def executable(path: Path, contents: bytes) -> None:
    path.write_bytes(contents)
    path.chmod(0o755)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        explicit = root / "explicit"
        nearby = root / "nearby"
        env_path = root / "env"
        path_path = root / "path"
        for path, contents in ((explicit, b"explicit"), (nearby, b"nearby"), (env_path, b"env"), (path_path, b"path")):
            executable(path, contents)

        selected = resolve_tool("elisa-lsp", root, explicit=explicit, trusted_nearby=[nearby], env_var="ELISA_LSP", env={"ELISA_LSP": str(env_path)}, path_lookup=lambda _: str(path_path))
        assert selected.path == explicit.resolve() and selected.selected_by == "explicit"
        assert len(selected.sha256) == 64 and selected.size == len(b"explicit")
        selected = resolve_tool("elisa-lsp", root, trusted_nearby=[nearby], env_var="ELISA_LSP", env={"ELISA_LSP": str(env_path)}, path_lookup=lambda _: str(path_path))
        assert selected.path == nearby.resolve() and selected.selected_by == "trusted nearby"
        selected = resolve_tool("elisa-lsp", root, env_var="ELISA_LSP", env={"ELISA_LSP": str(env_path)}, path_lookup=lambda _: str(path_path))
        assert selected.path == env_path.resolve() and selected.selected_by == "ELISA_LSP"
        selected = resolve_tool("elisa-lsp", root, env={}, path_lookup=lambda _: str(path_path))
        assert selected.path == path_path.resolve() and selected.selected_by == "PATH"
        assert resolve_tool("optional", root, env={}, path_lookup=lambda _: None, required=False) is None
        try:
            resolve_tool("elisa-lsp", root, explicit=root / "missing")
        except ToolchainError:
            pass
        else:
            raise AssertionError("invalid explicit tool silently fell through")
    print("ok toolchain resolver")


if __name__ == "__main__":
    main()
