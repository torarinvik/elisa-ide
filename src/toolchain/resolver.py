"""Safe executable discovery with content identity and compatibility facts."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


class ToolchainError(RuntimeError):
    """A required tool or compiler/runtime pairing is unavailable."""


@dataclass(frozen=True)
class ToolRecord:
    name: str
    path: Path
    selected_by: str
    sha256: str
    size: int
    host_arch: str
    version: str | None = None
    revision: str | None = None

    @property
    def compatibility(self) -> tuple[str, str, str]:
        return (self.name, self.host_arch, self.revision or self.sha256)


@dataclass(frozen=True)
class CompilerToolchain:
    root: Path
    stage1: ToolRecord
    runtime: ToolRecord
    revision: str | None

    @property
    def compatibility(self) -> tuple[str, str, str, str]:
        return (
            self.stage1.host_arch,
            self.revision or self.stage1.sha256,
            self.stage1.sha256,
            self.runtime.sha256,
        )


def _identity(path: Path) -> tuple[str, int]:
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
        raise ToolchainError(f"could not hash tool {path}: {exc}") from exc
    return digest.hexdigest(), size


def _canonical(path_text: str | os.PathLike[str], workspace_root: Path, source: str) -> Path:
    raw = Path(os.fspath(path_text)).expanduser()
    candidate = raw if raw.is_absolute() else workspace_root / raw
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ToolchainError(f"{source} tool does not exist: {candidate}: {exc}") from exc
    if not candidate.is_file():
        raise ToolchainError(f"{source} tool is not a regular file: {candidate}")
    if not os.access(candidate, os.X_OK):
        raise ToolchainError(f"{source} tool is not executable: {candidate}")
    return candidate


def _revision(path: Path) -> str | None:
    directory = path if path.is_dir() else path.parent
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(directory), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _record(name: str, path: Path, selected_by: str, *, version: str | None = None, revision: str | None = None) -> ToolRecord:
    sha256, size = _identity(path)
    return ToolRecord(
        name=name,
        path=path,
        selected_by=selected_by,
        sha256=sha256,
        size=size,
        host_arch=platform.machine(),
        version=version,
        revision=revision,
    )


def resolve_tool(
    name: str,
    workspace_root: str | os.PathLike[str],
    *,
    explicit: str | os.PathLike[str] | None = None,
    trusted_nearby: Iterable[str | os.PathLike[str]] = (),
    env_var: str | None = None,
    env: Mapping[str, str] | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
    path_name: str | None = None,
    required: bool = True,
) -> ToolRecord | None:
    """Select a tool using explicit, trusted-nearby, environment, PATH order."""

    root = Path(workspace_root).expanduser().resolve()
    variables = os.environ if env is None else env
    if explicit is not None:
        path = _canonical(explicit, root, "explicit")
        return _record(name, path, "explicit", revision=_revision(path))
    for nearby in trusted_nearby:
        candidate = Path(os.fspath(nearby)).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        if candidate.is_file():
            path = _canonical(candidate, root, "trusted nearby")
            return _record(name, path, "trusted nearby", revision=_revision(path))
    if env_var:
        configured = variables.get(env_var, "")
        if configured:
            path = _canonical(configured, root, env_var)
            return _record(name, path, env_var, revision=_revision(path))
    lookup = shutil.which if path_lookup is None else path_lookup
    selected = lookup(path_name or name)
    if selected:
        path = _canonical(selected, root, "PATH")
        return _record(name, path, "PATH", revision=_revision(path))
    if required:
        raise ToolchainError(f"required tool {name!r} was not found")
    return None


def resolve_compiler(
    workspace_root: str | os.PathLike[str],
    *,
    compiler_root: str | os.PathLike[str] | None = None,
) -> CompilerToolchain:
    """Resolve stage1 and runtime together so a stale pairing cannot pass."""

    root = Path(workspace_root).expanduser().resolve()
    selected_root = Path(compiler_root or (root / "compiler")).expanduser()
    if not selected_root.is_absolute():
        selected_root = root / selected_root
    try:
        selected_root = selected_root.resolve(strict=True)
    except OSError as exc:
        raise ToolchainError(f"compiler root does not exist: {selected_root}: {exc}") from exc
    stage1_path = _canonical(selected_root / "bin" / "elisac-stage1", root, "compiler stage1")
    runtime_path = selected_root / "build" / "runtime" / "elisacore_runtime.o"
    try:
        runtime_path = runtime_path.resolve(strict=True)
    except OSError as exc:
        raise ToolchainError(f"compiler runtime does not exist: {runtime_path}: {exc}") from exc
    if not runtime_path.is_file():
        raise ToolchainError(f"compiler runtime is not a regular file: {runtime_path}")
    revision = _revision(selected_root)
    return CompilerToolchain(
        root=selected_root,
        stage1=_record("elisac-stage1", stage1_path, "compiler root", revision=revision),
        runtime=_record("elisacore_runtime.o", runtime_path, "compiler root", revision=revision),
        revision=revision,
    )
