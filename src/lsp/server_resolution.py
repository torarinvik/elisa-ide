"""Deterministic, auditable Elisa-LSP executable discovery."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


class LspResolutionError(RuntimeError):
    """No usable language server could be selected."""


@dataclass(frozen=True)
class LspServer:
    """The exact executable selected for a workspace."""

    path: Path
    selected_by: str
    sha256: str
    size: int

    @property
    def command(self) -> tuple[str, ...]:
        return (os.fspath(self.path),)


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise LspResolutionError(f"could not read Elisa-LSP executable {path}: {exc}") from exc
    return digest.hexdigest(), size


def _candidate(path_text: str | os.PathLike[str], workspace_root: Path, selected_by: str) -> LspServer:
    raw = Path(os.fspath(path_text)).expanduser()
    path = raw if raw.is_absolute() else workspace_root / raw
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise LspResolutionError(f"{selected_by} Elisa-LSP does not exist: {path}: {exc}") from exc
    if not path.is_file():
        raise LspResolutionError(f"{selected_by} Elisa-LSP is not a regular file: {path}")
    if not os.access(path, os.X_OK):
        raise LspResolutionError(f"{selected_by} Elisa-LSP is not executable: {path}")
    sha256, size = _digest(path)
    return LspServer(path=path, selected_by=selected_by, sha256=sha256, size=size)


def resolve_server(
    workspace_root: str | os.PathLike[str],
    *,
    workspace_setting: str | os.PathLike[str] | None = None,
    trusted_nearby: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
) -> LspServer:
    """Resolve an Elisa-LSP executable in a fixed, reviewable order.

    A configured path or environment override is never silently ignored when
    it is present but invalid. Nearby builds are considered only when the
    caller explicitly supplies the trusted path; opening a folder cannot
    execute an arbitrary sibling binary by itself.
    """

    root = Path(workspace_root).expanduser().resolve()
    variables = os.environ if env is None else env
    if workspace_setting is not None:
        return _candidate(workspace_setting, root, "workspace setting")
    if trusted_nearby is not None:
        return _candidate(trusted_nearby, root, "trusted nearby build")
    configured_env = variables.get("ELISA_LSP", "")
    if configured_env:
        return _candidate(configured_env, root, "ELISA_LSP")
    lookup = shutil.which if path_lookup is None else path_lookup
    resolved = lookup("elisa-lsp")
    if resolved:
        return _candidate(resolved, root, "PATH")
    raise LspResolutionError(
        "Elisa-LSP was not found; set the workspace setting, provide a trusted nearby build, "
        "set ELISA_LSP, or install elisa-lsp on PATH"
    )
