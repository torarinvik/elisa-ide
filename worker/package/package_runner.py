#!/usr/bin/env python3
"""Discover and execute one local Elisa package test through elisapkg.

The shell uses ``plan`` as a read-only preview before it asks for trust.  The
subsequent ``run`` command recomputes the plan and requires its approval token,
so a changed manifest or package-manager binary cannot reuse an old approval.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import signal
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Mapping, NoReturn

HERE = Path(__file__).resolve()
MAX_MANIFEST_BYTES = 1 << 20
MAX_TEST_TARGETS = 64
MAX_SOURCE_PATH_BYTES = 4096
MAX_PACKAGE_FILES = 20_000
MAX_PACKAGE_INPUT_BYTES = 512 << 20
MAX_LOCAL_PACKAGES = 128
MAX_PACKAGE_HISTORY_RECORDS = 64
MAX_PACKAGE_HISTORY_RECORD_BYTES = 64 << 10
MAX_PACKAGE_HISTORY_DISPLAY_RECORDS = 8
MAX_PACKAGE_HISTORY_OUTPUT_PER_STREAM = 6 << 10
MAX_PACKAGE_TEST_CASES = 256
MAX_PACKAGE_TEST_CASE_OUTPUT_BYTES = 2048
MAX_PACKAGE_TEST_CASE_OUTPUT_TOTAL_BYTES = 12 << 10
STALE_SUCCESS_EXIT = 250
STALE_FAILURE_EXIT = 251
_PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_PACKAGE_TASK_ID = re.compile(r"[0-9TZ.-]+-[0-9a-f]{12}$")
_PACKAGE_HISTORY_TIMESTAMP = re.compile(r"[0-9T:.-]+Z$")
_TEST_CASE_STATUS_LINE = re.compile(r"^\[\s*(RUN|OK|SKIPPED|FAILED|PANIC)\s*\]\s+([^\s]+)(?:\s+(.*))?$")
_TEST_CASE_OUTPUT_LINE = re.compile(r"^\[\s*(STDOUT|STDERR)\s*\]\s+([^\s]+)\s*$")
_TEST_CASE_SUMMARY_LINE = re.compile(r"^\[\s*SUMMARY\s*\]\s+(\d+) test\(s\) selected; passed=(\d+) skipped=(\d+) failed=(\d+)\s*$")
_SNAPSHOT_IGNORED_DIRECTORIES = {".git", ".elisa-ide", ".cache", "build", "target", "__pycache__"}
_ACTIVE_PACKAGE_PROCESS: subprocess.Popen[bytes] | None = None
_PACKAGE_GROUP_TERMINATION_SENT = False
_PACKAGE_CANCEL_SIGNAL: int | None = None


class PackageTaskError(RuntimeError):
    """A package test cannot be safely planned or launched."""


def _ide_root() -> Path:
    for candidate in (HERE.parent, *HERE.parents):
        if (candidate / "scripts" / "build_ide.sh").is_file():
            return candidate.resolve()
    # In a packaged app, local tools are selected from ELISAPKG or PATH.
    return HERE.parent.resolve()


def _load_resolver():
    root = _ide_root()
    library = HERE.parent / "package_lib"
    if (library / "src" / "toolchain" / "resolver.py").is_file():
        sys.path.insert(0, os.fspath(library))
    else:
        sys.path.insert(0, os.fspath(root))
    from src.toolchain.resolver import ToolRecord, ToolchainError, resolve_tool

    return root, ToolRecord, ToolchainError, resolve_tool


def _regular_non_symlink(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PackageTaskError(f"could not inspect {label} {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PackageTaskError(f"{label} must be a regular, non-symlink file: {path}")
    return path.resolve(strict=True)


def _find_manifest(source_text: str) -> tuple[Path, Path]:
    raw_source = Path(source_text).expanduser()
    try:
        source = raw_source.resolve(strict=True)
    except OSError as exc:
        raise PackageTaskError(f"source file does not exist: {raw_source}: {exc}") from exc
    if source.suffix != ".elisa" or not source.is_file():
        raise PackageTaskError("package tests require an opened saved .elisa source file")
    if len(os.fspath(source).encode("utf-8")) > MAX_SOURCE_PATH_BYTES:
        raise PackageTaskError("source path exceeds the package runner limit")

    directory = source.parent
    for _ in range(64):
        candidate = directory / "elisapkg.json"
        if candidate.exists() or candidate.is_symlink():
            manifest = _regular_non_symlink(candidate, "package manifest")
            return directory.resolve(strict=True), manifest
        parent = directory.parent
        if parent == directory:
            break
        directory = parent
    raise PackageTaskError("no elisapkg.json was found above the opened source file")


def _read_manifest_document(manifest: Path) -> tuple[bytes, dict[str, object]]:
    _regular_non_symlink(manifest, "package manifest")
    try:
        metadata = manifest.stat()
        if metadata.st_size <= 0 or metadata.st_size > MAX_MANIFEST_BYTES:
            raise PackageTaskError("elisapkg.json is empty or exceeds the 1 MiB manifest limit")
        raw = manifest.read_bytes()
        if len(raw) != metadata.st_size:
            raise PackageTaskError("elisapkg.json changed while it was being read")
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text)
    except PackageTaskError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageTaskError(f"could not read elisapkg.json: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema-version") != 1:
        raise PackageTaskError("elisapkg.json must use schema-version 1")
    package = value.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("name"), str):
        raise PackageTaskError("elisapkg.json has no valid package name")
    dependencies = value.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise PackageTaskError("elisapkg.json dependencies must be an object")
    for name, dependency in dependencies.items():
        if not isinstance(name, str) or not name or not isinstance(dependency, dict):
            raise PackageTaskError("elisapkg.json contains an unsupported dependency declaration")
        if set(dependency) != {"path"} or not isinstance(dependency.get("path"), str):
            raise PackageTaskError(
                "the IDE package task currently supports local path dependencies only; "
                f"dependency {name!r} is not a local path"
            )
        if not dependency["path"] or "\x00" in dependency["path"]:
            raise PackageTaskError(f"dependency {name!r} has an invalid local path")
    return raw, value


def _decode_manifest(manifest: Path) -> tuple[bytes, dict[str, object], tuple[str, ...]]:
    raw, value = _read_manifest_document(manifest)
    dev_dependencies = value.get("dev-dependencies", {})
    if not isinstance(dev_dependencies, dict) or dev_dependencies:
        raise PackageTaskError("the current elisapkg test profile requires empty dev-dependencies")

    targets = value.get("targets")
    if not isinstance(targets, list):
        raise PackageTaskError("elisapkg.json targets must be an array")
    tests: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise PackageTaskError("elisapkg.json contains a malformed target")
        if target.get("kind") != "test":
            continue
        name = target.get("name")
        if not isinstance(name, str) or not _PACKAGE_NAME.fullmatch(name):
            raise PackageTaskError("elisapkg.json contains an invalid test target name")
        if name in seen:
            raise PackageTaskError(f"elisapkg.json repeats test target {name!r}")
        seen.add(name)
        tests.append(name)
        if len(tests) > MAX_TEST_TARGETS:
            raise PackageTaskError(f"package has more than {MAX_TEST_TARGETS} test targets")
    if not tests:
        raise PackageTaskError("elisapkg.json declares no test targets")
    return raw, value, tuple(tests)


def _dependency_roots(root: Path, document: dict[str, object]) -> tuple[tuple[Path, dict[str, object]], ...]:
    packages: list[tuple[Path, dict[str, object]]] = [(root, document)]
    seen = {root.resolve(strict=True)}
    cursor = 0
    while cursor < len(packages):
        package_root, package_document = packages[cursor]
        cursor += 1
        dependencies = package_document.get("dependencies", {})
        if not isinstance(dependencies, dict):
            raise PackageTaskError(f"{package_root / 'elisapkg.json'} dependencies must be an object")
        for name, dependency in sorted(dependencies.items()):
            if not isinstance(name, str) or not isinstance(dependency, dict):
                raise PackageTaskError("elisapkg.json contains an unsupported dependency declaration")
            relative_path = dependency.get("path")
            if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
                raise PackageTaskError(f"dependency {name!r} has an invalid local path")
            requested_root = Path(relative_path).expanduser()
            if not requested_root.is_absolute():
                requested_root = package_root / requested_root
            try:
                requested_metadata = requested_root.lstat()
                if stat.S_ISLNK(requested_metadata.st_mode) or not stat.S_ISDIR(requested_metadata.st_mode):
                    raise PackageTaskError(f"dependency {name!r} must be a local, non-symlink directory")
                dependency_root = requested_root.resolve(strict=True)
                dependency_manifest = dependency_root / "elisapkg.json"
                _regular_non_symlink(dependency_manifest, f"dependency {name!r} manifest")
                _raw_manifest, dependency_document = _read_manifest_document(dependency_manifest)
            except PackageTaskError:
                raise
            except OSError as exc:
                raise PackageTaskError(f"could not inspect local dependency {name!r}: {exc}") from exc
            if dependency_root in seen:
                continue
            seen.add(dependency_root)
            packages.append((dependency_root, dependency_document))
            if len(packages) > MAX_LOCAL_PACKAGES:
                raise PackageTaskError(f"local package graph exceeds {MAX_LOCAL_PACKAGES} packages")
    return tuple(packages)


def _package_graph_digest(root: Path, document: dict[str, object]) -> str:
    packages = _dependency_roots(root, document)
    digest = hashlib.sha256(b"elisa-ide-package-input-tree-v1\0")
    total_files = 0
    total_bytes = 0

    def add_record(kind: bytes, package_root: Path, relative: str, mode: int, size: int, content: bytes) -> None:
        encoded_root = os.fsencode(package_root)
        encoded_relative = os.fsencode(relative)
        digest.update(kind)
        digest.update(len(encoded_root).to_bytes(4, "little"))
        digest.update(encoded_root)
        digest.update(len(encoded_relative).to_bytes(4, "little"))
        digest.update(encoded_relative)
        digest.update(mode.to_bytes(4, "little"))
        digest.update(size.to_bytes(8, "little"))
        digest.update(content)

    for package_root, _package_document in sorted(packages, key=lambda item: os.fsencode(item[0])):
        add_record(b"P", package_root, ".", 0, 0, b"")
        stack: list[tuple[Path, str]] = [(package_root, "")]
        while stack:
            directory, relative_directory = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name), reverse=True)
            except OSError as exc:
                raise PackageTaskError(f"could not read package input directory {directory}: {exc}") from exc
            for entry in entries:
                if not relative_directory and entry.name in _SNAPSHOT_IGNORED_DIRECTORIES:
                    continue
                relative = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
                if len(os.fsencode(relative)) > MAX_SOURCE_PATH_BYTES:
                    raise PackageTaskError("package input path exceeds the package runner limit")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise PackageTaskError(f"could not inspect package input {entry.path}: {exc}") from exc
                if stat.S_ISLNK(metadata.st_mode):
                    raise PackageTaskError(f"package input tree contains a symlink: {entry.path}")
                if stat.S_ISDIR(metadata.st_mode):
                    add_record(b"D", package_root, relative, stat.S_IMODE(metadata.st_mode), 0, b"")
                    stack.append((Path(entry.path), relative))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise PackageTaskError(f"package input is not a regular file: {entry.path}")
                total_files += 1
                total_bytes += metadata.st_size
                if total_files > MAX_PACKAGE_FILES:
                    raise PackageTaskError(f"package input tree exceeds {MAX_PACKAGE_FILES} files")
                if total_bytes > MAX_PACKAGE_INPUT_BYTES:
                    raise PackageTaskError(f"package input tree exceeds {MAX_PACKAGE_INPUT_BYTES} bytes")
                file_digest = hashlib.sha256()
                try:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(entry.path, flags)
                    with os.fdopen(descriptor, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                            metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns
                        ):
                            raise PackageTaskError(f"package input changed while being fingerprinted: {entry.path}")
                        while chunk := stream.read(64 << 10):
                            file_digest.update(chunk)
                        after = os.fstat(stream.fileno())
                        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
                        ):
                            raise PackageTaskError(f"package input changed while being fingerprinted: {entry.path}")
                except PackageTaskError:
                    raise
                except OSError as exc:
                    raise PackageTaskError(f"could not hash package input {entry.path}: {exc}") from exc
                add_record(b"F", package_root, relative, stat.S_IMODE(metadata.st_mode), metadata.st_size, file_digest.digest())
    return digest.hexdigest()


def prepare_plan(
    source_text: str,
    *,
    env: Mapping[str, str] | None = None,
    ide_root: str | os.PathLike[str] | None = None,
    trusted_nearby: tuple[str | os.PathLike[str], ...] | None = None,
) -> dict[str, object]:
    root, manifest = _find_manifest(source_text)
    raw_manifest, document, tests = _decode_manifest(manifest)
    resolver_root, _record_type, ToolchainError, resolve_tool = _load_resolver()
    selected_root = Path(ide_root).expanduser().resolve() if ide_root is not None else resolver_root
    variables = os.environ if env is None else env
    nearby: list[str | os.PathLike[str]]
    if trusted_nearby is not None:
        nearby = list(trusted_nearby)
    elif (selected_root / "scripts" / "build_ide.sh").is_file():
        nearby = [
            selected_root.parent / "elisa-pkg" / "build" / "elisapkg",
            selected_root.parent / "Elisa-pkg" / "build" / "elisapkg",
        ]
    else:
        nearby = []
    try:
        explicit_package_manager = variables.get("ELISA_IDE_ELISAPKG", "")
        tool = resolve_tool(
            "elisapkg",
            selected_root,
            explicit=explicit_package_manager or None,
            trusted_nearby=nearby,
            env_var="ELISAPKG",
            env=variables,
            path_name="elisapkg",
            required=True,
        )
    except ToolchainError as exc:
        raise PackageTaskError(str(exc)) from exc
    assert tool is not None
    manifest_digest = hashlib.sha256(raw_manifest).hexdigest()
    input_tree_digest = _package_graph_digest(root, document)
    approval = hashlib.sha256(
        b"elisa-ide-package-trust-v1\0"
        + os.fsencode(root)
        + b"\0"
        + manifest_digest.encode("ascii")
        + b"\0"
        + input_tree_digest.encode("ascii")
        + b"\0"
        + os.fsencode(tool.path)
        + b"\0"
        + tool.sha256.encode("ascii")
    ).hexdigest()
    package = document["package"]
    assert isinstance(package, dict)
    return {
        "root": root,
        "manifest": manifest,
        "manifest_digest": manifest_digest,
        "input_tree_digest": input_tree_digest,
        "package_name": package["name"],
        "tests": tests,
        "tool": tool,
        "approval": approval,
    }


def format_plan(plan: dict[str, object]) -> str:
    root = plan["root"]
    manifest = plan["manifest"]
    tool = plan["tool"]
    tests = plan["tests"]
    assert isinstance(root, Path) and isinstance(manifest, Path)
    assert isinstance(tests, tuple) and tests
    first = tests[0]
    assert isinstance(first, str)
    lines = [
        f"Package: {plan['package_name']}",
        f"Manifest: {manifest}",
        f"Package input SHA-256: {plan['input_tree_digest']}",
        f"Package manager: {tool.path}",
        f"Package manager SHA-256: {tool.sha256}",
        f"Discovered test targets ({len(tests)}): {', '.join(tests)}",
        f"Default target: {first}",
        f"Command: {tool.path} test --offline --test {first}",
        f"Working directory: {root}",
        "Registry dependencies are refused by the IDE test profile; local path dependencies only.",
        "No project code has run.",
        f"PLAN1 {plan['approval']} {first}",
        f"TARGETS1 {' '.join(tests)}",
    ]
    return "\n".join(lines) + "\n"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _package_history_directory(root: Path, *, create: bool) -> Path | None:
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise PackageTaskError(f"could not resolve package root for test history {root}: {exc}") from exc
    project_metadata = root / ".elisa-ide"
    history = project_metadata / "package-test-history"
    for directory in (project_metadata, history):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                directory.mkdir(mode=0o700)
                metadata = directory.lstat()
            except OSError as exc:
                raise PackageTaskError(f"could not create private package test history directory {directory}: {exc}") from exc
        except OSError as exc:
            raise PackageTaskError(f"could not inspect package test history directory {directory}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PackageTaskError(f"package test history path must be a real directory: {directory}")
        try:
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise PackageTaskError(f"could not resolve package test history directory {directory}: {exc}") from exc
        if not resolved.is_relative_to(root):
            raise PackageTaskError(f"package test history directory escapes the package root: {directory}")
    if create:
        try:
            os.chmod(history, 0o700)
        except OSError as exc:
            raise PackageTaskError(f"could not protect package test history directory {history}: {exc}") from exc
    return history


def _read_package_history_files(directory: Path) -> list[dict[str, object]]:
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise PackageTaskError(f"could not open package test history directory {directory}: {exc}") from exc
    records: list[dict[str, object]] = []
    try:
        with os.scandir(directory_fd) as entries:
            names = [entry.name for entry in entries if entry.name.endswith(".task.json")]
        for name in names:
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                with os.fdopen(descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > MAX_PACKAGE_HISTORY_RECORD_BYTES:
                        continue
                    raw = stream.read(MAX_PACKAGE_HISTORY_RECORD_BYTES + 1)
                if len(raw) > MAX_PACKAGE_HISTORY_RECORD_BYTES:
                    continue
                value = json.loads(raw.decode("utf-8", errors="strict"))
                if not isinstance(value, dict) or value.get("schema_version") != 1:
                    continue
                if value.get("state") not in {"started", "succeeded", "failed", "stale", "cancelled"}:
                    continue
                if not isinstance(value.get("started_at"), str) or not isinstance(value.get("target"), str):
                    continue
                if not _PACKAGE_TASK_ID.fullmatch(str(value.get("task_id", ""))):
                    continue
                if not _PACKAGE_HISTORY_TIMESTAMP.fullmatch(value["started_at"]):
                    continue
                if len(value["started_at"]) > 40 or not _PACKAGE_NAME.fullmatch(value["target"]):
                    continue
                records.append(value)
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
        records.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return records
    finally:
        os.close(directory_fd)


def _prune_package_history(root: Path, directory: Path) -> None:
    try:
        records = _read_package_history_files(directory)
        retained = {str(item.get("task_id", "")) + ".task.json" for item in records[:MAX_PACKAGE_HISTORY_RECORDS]}
        with os.scandir(directory) as entries:
            candidates = [entry.name for entry in entries if entry.name.endswith(".task.json")]
        for name in candidates:
            if name in retained:
                continue
            candidate = directory / name
            try:
                metadata = candidate.lstat()
                if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                    candidate.unlink()
            except OSError:
                continue
    except PackageTaskError:
        return


def _write_package_history_record(root: Path, record: dict[str, object]) -> Path:
    directory = _package_history_directory(root, create=True)
    assert directory is not None
    try:
        raw = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PackageTaskError(f"package test history contains invalid data: {exc}") from exc
    if len(raw) > MAX_PACKAGE_HISTORY_RECORD_BYTES:
        raise PackageTaskError(f"package test history record exceeds {MAX_PACKAGE_HISTORY_RECORD_BYTES} bytes")
    task_id = record.get("task_id")
    if not isinstance(task_id, str) or not _PACKAGE_TASK_ID.fullmatch(task_id):
        raise PackageTaskError("package test history task id is invalid")
    final_name = task_id + ".task.json"
    temporary_name = "." + task_id + "." + secrets.token_hex(6) + ".tmp"
    directory_fd: int | None = None
    descriptor: int | None = None
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, final_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise PackageTaskError(f"could not save package test history in {directory}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
            os.close(directory_fd)
    path = directory / final_name
    _prune_package_history(root, directory)
    return path


def _new_package_history_record(plan: dict[str, object], target: str, argv: list[str], environment: Mapping[str, str]) -> dict[str, object]:
    started = datetime.now(timezone.utc)
    task_id = started.strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(6)
    tool = plan["tool"]
    root = plan["root"]
    manifest = plan["manifest"]
    assert isinstance(root, Path) and isinstance(manifest, Path)
    variable_names = sorted(str(name) for name in environment)[:128]
    return {
        "schema_version": 1,
        "task_id": task_id,
        "state": "started",
        "started_at": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "finished_at": None,
        "package": {
            "name": plan["package_name"],
            "root": os.fspath(root),
            "manifest": os.fspath(manifest),
            "manifest_sha256": plan["manifest_digest"],
            "input_tree_sha256": plan["input_tree_digest"],
        },
        "target": target,
        "launch": {
            "argv": list(argv),
            "working_directory": os.fspath(root),
            "environment_variable_names": variable_names,
            "environment_variable_names_truncated": len(environment) > len(variable_names),
        },
        "package_manager": {
            "path": os.fspath(tool.path),
            "sha256": tool.sha256,
            "selected_by": tool.selected_by,
        },
        "python": {"path": sys.executable, "version": sys.version.split()[0]},
        "compiler": None,
        "result": None,
        "error": None,
    }


def _safe_history_text(text: str) -> str:
    return "".join(
        character if character in "\n\t" or ord(character) >= 32 and ord(character) != 127
        else f"\\x{ord(character):02x}"
        for character in text
    )


def _history_output_text(record: dict[str, object], stream_name: str) -> tuple[str, bool]:
    result = record.get("result")
    if not isinstance(result, dict):
        return "", False
    encoded = result.get(f"{stream_name}_base64")
    if not isinstance(encoded, str):
        return "", False
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return "[invalid saved output]\n", False
    return _safe_history_text(raw.decode("utf-8", errors="replace")), result.get(f"{stream_name}_truncated") is True


def _parse_test_case_results(output: bytes) -> tuple[list[dict[str, object]], dict[str, int] | None, int]:
    """Read the line-oriented report emitted by Elisa's ``-emit test`` runner.

    Unrecognized compiler or package-manager output remains in the ordinary stream
    capture. Only exact status and capture headers create per-case records.
    """
    cases: list[dict[str, object]] = []
    selected_summary: dict[str, int] | None = None
    omitted_cases = 0
    current_index: int | None = None
    capture_index: int | None = None
    capture_stream: str | None = None
    capture_open = False
    output_bytes_used = 0
    status_names = {"RUN": "running", "OK": "passed", "SKIPPED": "skipped", "FAILED": "failed", "PANIC": "panic"}

    for line in output.decode("utf-8", errors="replace").splitlines():
        summary_match = _TEST_CASE_SUMMARY_LINE.fullmatch(line)
        if summary_match is not None:
            selected_summary = {
                "selected": int(summary_match.group(1)),
                "passed": int(summary_match.group(2)),
                "skipped": int(summary_match.group(3)),
                "failed": int(summary_match.group(4)),
            }
            capture_open = False
            continue

        status_match = _TEST_CASE_STATUS_LINE.fullmatch(line)
        if status_match is not None:
            status = status_match.group(1)
            name = status_match.group(2)
            detail = status_match.group(3) or ""
            capture_open = False
            capture_index = None
            capture_stream = None
            if status == "RUN":
                if len(cases) >= MAX_PACKAGE_TEST_CASES:
                    omitted_cases += 1
                    current_index = None
                    continue
                cases.append({"name": name[:256], "state": "running", "detail": "", "output": "", "output_truncated": False})
                current_index = len(cases) - 1
            else:
                matching_index = current_index
                if matching_index is None or cases[matching_index]["name"] != name:
                    matching_index = next((index for index in range(len(cases) - 1, -1, -1) if cases[index]["name"] == name and cases[index]["state"] == "running"), None)
                if matching_index is not None:
                    cases[matching_index]["state"] = status_names[status]
                    cases[matching_index]["detail"] = detail[:512]
                    current_index = matching_index
            continue

        output_match = _TEST_CASE_OUTPUT_LINE.fullmatch(line)
        if output_match is not None:
            name = output_match.group(2)
            matching_index = next((index for index in range(len(cases) - 1, -1, -1) if cases[index]["name"] == name), None)
            if matching_index is not None:
                capture_index = matching_index
                capture_stream = output_match.group(1).lower()
                capture_open = True
                heading = f"{capture_stream}:\n"
                record = cases[matching_index]
                prior = str(record["output"])
                encoded_heading = heading.encode("utf-8")
                if output_bytes_used + len(encoded_heading) <= MAX_PACKAGE_TEST_CASE_OUTPUT_TOTAL_BYTES:
                    record["output"] = prior + heading
                    output_bytes_used += len(encoded_heading)
                else:
                    record["output_truncated"] = True
            continue

        if capture_open and capture_index is not None and capture_stream is not None and line.startswith("    "):
            record = cases[capture_index]
            text = line[4:]
            encoded = (text + "\n").encode("utf-8")
            case_output = str(record["output"])
            case_bytes = len(case_output.encode("utf-8"))
            available = min(MAX_PACKAGE_TEST_CASE_OUTPUT_BYTES - case_bytes, MAX_PACKAGE_TEST_CASE_OUTPUT_TOTAL_BYTES - output_bytes_used)
            if available > 0:
                accepted = encoded[:available].decode("utf-8", errors="ignore")
                record["output"] = case_output + accepted
                output_bytes_used += len(accepted.encode("utf-8"))
            if len(encoded) > available:
                record["output_truncated"] = True
            continue
        capture_open = False

    return cases, selected_summary, omitted_cases


def format_package_test_explorer(source_text: str, selection: int, case_selection: int) -> str:
    root, _manifest = _find_manifest(source_text)
    directory = _package_history_directory(root, create=False)
    if directory is None:
        return "No package test history is recorded for this workspace.\n"
    records = _read_package_history_files(directory)[:MAX_PACKAGE_HISTORY_DISPLAY_RECORDS]
    if type(selection) is not int or selection < 0:
        raise PackageTaskError("selected package test history index is invalid")
    if selection >= len(records):
        return f"No package test run is available at position {selection + 1}.\nUse H to return to the recent-run list.\n"
    record = records[selection]
    result = record.get("result")
    cases = result.get("test_cases") if isinstance(result, dict) else None
    if not isinstance(cases, list) or not cases:
        return (
            f"Package test explorer — run {selection + 1} of {len(records)}\n"
            f"Target: {record.get('target', 'unknown')} | state: {record.get('state', 'unknown')}\n"
            "This run did not report structured Elisa @test cases. Its complete bounded output is available with H, then J/K.\n"
        )
    valid_cases = [case for case in cases if isinstance(case, dict) and isinstance(case.get("name"), str) and isinstance(case.get("state"), str)]
    if not valid_cases:
        return "This package test record has no valid structured case results.\n"
    if type(case_selection) is not int or not 0 <= case_selection < len(valid_cases):
        raise PackageTaskError("selected Elisa test case index is invalid")
    summary = result.get("test_summary") if isinstance(result, dict) else None
    lines = [
        f"Package test explorer — run {selection + 1} of {len(records)}",
        f"Target: {record.get('target', 'unknown')} | task state: {record.get('state', 'unknown')}",
        f"Reported cases: {len(valid_cases)}" + (f" | summary: {summary.get('passed', 0)} passed, {summary.get('skipped', 0)} skipped, {summary.get('failed', 0)} failed" if isinstance(summary, dict) else ""),
        "Cases:",
    ]
    if isinstance(result, dict) and type(result.get("omitted_test_cases")) is int and result["omitted_test_cases"] > 0:
        lines.append(f"[additional cases omitted by the {MAX_PACKAGE_TEST_CASES}-case safety limit: {result['omitted_test_cases']}]")
    for index, case in enumerate(valid_cases):
        state = str(case["state"]).upper()
        marker = ">" if index == case_selection else " "
        name = _safe_history_text(str(case["name"]))
        detail = _safe_history_text(str(case.get("detail") or ""))
        lines.append(f"{marker} {index + 1}. {state} {name}" + (f" — {detail}" if detail else ""))
    selected = valid_cases[case_selection]
    lines.extend(("", f"Selected case {case_selection + 1} of {len(valid_cases)}: {_safe_history_text(str(selected['name']))}", f"State: {str(selected['state']).upper()}"))
    detail = selected.get("detail")
    if isinstance(detail, str) and detail:
        lines.append(f"Result detail: {_safe_history_text(detail)}")
    output_text = selected.get("output")
    lines.extend(("--- captured case output ---", _safe_history_text(output_text.rstrip("\n")) if isinstance(output_text, str) and output_text else "(no per-case output was reported)"))
    if selected.get("output_truncated") is True:
        lines.append("[per-case output truncated by the IDE safety limit]")
    lines.append("Use Ctrl/Cmd+Option+Shift+J/K to inspect the previous/next case; E returns to the selected run detail; H opens run history.")
    return "\n".join(lines) + "\n"


def format_package_history(source_text: str, selection: int | None = None) -> str:
    root, _manifest = _find_manifest(source_text)
    directory = _package_history_directory(root, create=False)
    if directory is None:
        return "No package test history is recorded for this workspace.\n"
    records = _read_package_history_files(directory)
    if not records:
        return "No package test history is recorded for this workspace.\n"
    visible_records = records[:MAX_PACKAGE_HISTORY_DISPLAY_RECORDS]
    if selection is not None:
        if type(selection) is not int or selection < 0:
            raise PackageTaskError("selected package test history index is invalid")
        if selection >= len(visible_records):
            return f"No package test run is available at position {selection + 1}.\nUse H to return to the recent-run list.\n"
        record = visible_records[selection]
        result = record.get("result")
        exit_code = result.get("package_manager_exit_code") if isinstance(result, dict) else None
        exit_text = f"{exit_code}" if type(exit_code) is int else "unavailable"
        lines = [
            f"Package test run {selection + 1} of {len(visible_records)} (newest first)",
            f"Task: {record.get('task_id', 'unknown')}",
            f"Started: {record.get('started_at', 'unknown')}",
            f"Finished: {record.get('finished_at') or 'still running or unavailable'}",
            f"State: {record.get('state', 'unknown')} | target: {record.get('target', 'unknown')} | package-manager exit: {exit_text}",
        ]
        launch = record.get("launch")
        if isinstance(launch, dict):
            working_directory = launch.get("working_directory", "unavailable")
            lines.append(f"Working directory: {_safe_history_text(str(working_directory))}")
            argv = launch.get("argv")
            if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
                lines.append(f"Command: {_safe_history_text(repr(argv))}")
        package = record.get("package")
        if isinstance(package, dict):
            input_digest = package.get("input_tree_sha256", "unavailable")
            lines.append(f"Package input SHA-256: {_safe_history_text(str(input_digest))}")
        for stream_name in ("stdout", "stderr"):
            output, truncated = _history_output_text(record, stream_name)
            lines.extend((f"--- {stream_name} ---", output.rstrip("\n") or "(no output)"))
            if truncated:
                lines.append(f"[{stream_name} history capture truncated at {MAX_PACKAGE_HISTORY_OUTPUT_PER_STREAM} bytes]")
        if isinstance(result, dict) and result.get("output_capture_truncated") is True:
            lines.append("[combined stdout/stderr history capture reached its size limit]")
        cases = result.get("test_cases") if isinstance(result, dict) else None
        if isinstance(cases, list) and cases:
            counts: dict[str, int] = {}
            for case in cases:
                if isinstance(case, dict) and isinstance(case.get("state"), str):
                    state = str(case["state"])
                    counts[state] = counts.get(state, 0) + 1
            lines.append(f"Structured Elisa test cases: {len(cases)} ({counts.get('passed', 0)} passed, {counts.get('skipped', 0)} skipped, {counts.get('failed', 0) + counts.get('panic', 0)} failed/panicked)")
            lines.append("Use Ctrl/Cmd+Option+Shift+E to open the per-case explorer.")
        lines.append("Use Ctrl/Cmd+Option+Shift+J/K to inspect older/newer runs; H shows the run list.")
        return "\n".join(lines) + "\n"

    lines = ["Recent package test runs (newest first):"]
    for index, record in enumerate(visible_records):
        timestamp = str(record.get("started_at") or "unknown")
        timestamp = timestamp[:19] + "Z" if len(timestamp) >= 19 else timestamp
        result = record.get("result")
        exit_code = result.get("package_manager_exit_code") if isinstance(result, dict) else None
        exit_text = f"exit {exit_code}" if type(exit_code) is int else "no exit code"
        lines.append(f"{index + 1}. {timestamp} | {record['state']} | {record['target']} | {exit_text}")
    lines.append("Use Ctrl/Cmd+Option+Shift+J/K to inspect each run's output.")
    return "\n".join(lines) + "\n"


def _cancel_package_process(signum: int, _frame: object) -> None:
    global _PACKAGE_GROUP_TERMINATION_SENT
    global _PACKAGE_CANCEL_SIGNAL
    process = _ACTIVE_PACKAGE_PROCESS
    _PACKAGE_CANCEL_SIGNAL = signum
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        _PACKAGE_GROUP_TERMINATION_SENT = True
    return


def _finish_package_history(
    root: Path,
    record: dict[str, object] | None,
    state: str,
    package_exit_code: int | None,
    *,
    stale_reason: str | None = None,
    error: str | None = None,
    stdout: bytes = b"",
    stderr: bytes = b"",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    output_capture_truncated: bool = False,
) -> Path | None:
    if record is None:
        return None
    record["state"] = state
    record["finished_at"] = _timestamp()
    test_cases, test_summary, omitted_test_cases = _parse_test_case_results(stdout)
    record["result"] = {
        "package_manager_exit_code": package_exit_code,
        "ide_exit_code": (
            STALE_SUCCESS_EXIT if state == "stale" and package_exit_code == 0
            else STALE_FAILURE_EXIT if state == "stale"
            else 0 if state == "succeeded"
            else 1 if state == "failed"
            else None
        ),
        "stale": state == "stale",
        "stale_reason": stale_reason,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_capture_truncated": output_capture_truncated,
        "test_cases": test_cases,
        "test_summary": test_summary,
        "omitted_test_cases": omitted_test_cases,
    }
    record["error"] = error
    try:
        root_value = record.get("package")
        assert isinstance(root_value, dict)
        package_root = Path(str(root_value["root"]))
        return _write_package_history_record(package_root, record)
    except (PackageTaskError, OSError, AssertionError) as exc:
        os.write(2, f"package test history: could not save terminal state: {exc}\n".encode("utf-8", errors="replace"))
        return None


def _stop_package_process(process: subprocess.Popen[bytes]) -> None:
    global _PACKAGE_GROUP_TERMINATION_SENT
    if process.poll() is not None:
        return
    if not _PACKAGE_GROUP_TERMINATION_SENT:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=0.3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.kill()
    process.wait()
    _PACKAGE_GROUP_TERMINATION_SENT = False


def run_test(
    source_text: str,
    target: str,
    approval: str,
    *,
    env: Mapping[str, str] | None = None,
    ide_root: str | os.PathLike[str] | None = None,
    trusted_nearby: tuple[str | os.PathLike[str], ...] | None = None,
) -> int:
    plan = prepare_plan(source_text, env=env, ide_root=ide_root, trusted_nearby=trusted_nearby)
    if approval != plan["approval"]:
        raise PackageTaskError("package or elisapkg changed after the trust preview; review the plan again")
    tests = plan["tests"]
    if not isinstance(target, str) or target not in tests:
        raise PackageTaskError("selected test target is no longer present in elisapkg.json")
    tool = plan["tool"]
    root = plan["root"]
    assert isinstance(root, Path)
    argv = [os.fspath(tool.path), "test", "--offline", "--test", target]
    os.write(1, f"Package test task: {plan['package_name']} / {target}\n".encode("utf-8"))
    os.write(1, f"Executable: {tool.path} (sha256 {tool.sha256})\n".encode("utf-8"))
    os.write(1, f"Working directory: {root}\n".encode("utf-8"))
    os.write(1, f"Argv: {argv!r}\n\n".encode("utf-8"))
    environment = dict(os.environ if env is None else env)
    history_record: dict[str, object] | None = None
    history_path: Path | None = None
    try:
        history_record = _new_package_history_record(plan, target, argv, environment)
        history_path = _write_package_history_record(root, history_record)
        os.write(1, f"Task record: {history_path}\n".encode("utf-8", errors="replace"))
    except PackageTaskError as exc:
        history_record = None
        os.write(2, f"package test history: {exc}; test will continue without a saved record\n".encode("utf-8", errors="replace"))
    global _ACTIVE_PACKAGE_PROCESS
    global _PACKAGE_GROUP_TERMINATION_SENT
    global _PACKAGE_CANCEL_SIGNAL
    _ACTIVE_PACKAGE_PROCESS = None
    _PACKAGE_GROUP_TERMINATION_SENT = False
    _PACKAGE_CANCEL_SIGNAL = None
    try:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=environment,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except BaseException as exc:
        _finish_package_history(root, history_record, "failed", None, error=type(exc).__name__)
        raise
    _ACTIVE_PACKAGE_PROCESS = process
    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    try:
        signal.signal(signal.SIGTERM, _cancel_package_process)
        signal.signal(signal.SIGINT, _cancel_package_process)
        captures = {"stdout": bytearray(), "stderr": bytearray()}
        capture_truncated = {"stdout": False, "stderr": False}
        try:
            with selectors.DefaultSelector() as selector:
                for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    if stream is None:
                        continue
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, stream_name)
                while selector.get_map():
                    if _PACKAGE_CANCEL_SIGNAL is not None and process.poll() is None:
                        _stop_package_process(process)
                    events = selector.select(timeout=0.05)
                    if not events and process.poll() is not None:
                        break
                    for key, _mask in events:
                        stream_name = str(key.data)
                        descriptor = key.fileobj.fileno()
                        try:
                            chunk = os.read(descriptor, 4096)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        buffer = captures[stream_name]
                        remaining = MAX_PACKAGE_HISTORY_OUTPUT_PER_STREAM - len(buffer)
                        if remaining > 0:
                            buffer.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            capture_truncated[stream_name] = True
                        output_descriptor = 1 if stream_name == "stdout" else 2
                        view = memoryview(chunk)
                        while view:
                            try:
                                written = os.write(output_descriptor, view)
                            except InterruptedError:
                                continue
                            except OSError:
                                # The UI may have closed its output pipe. Keep draining
                                # and record the bounded history before exiting.
                                break
                            if written <= 0:
                                break
                            view = view[written:]
                package_exit_code = process.wait()
        except BaseException as exc:
            _stop_package_process(process)
            state = "cancelled" if _PACKAGE_CANCEL_SIGNAL is not None else "failed"
            _finish_package_history(
                root,
                history_record,
                state,
                process.returncode,
                error=type(exc).__name__,
                stdout=bytes(captures["stdout"]),
                stderr=bytes(captures["stderr"]),
                stdout_truncated=capture_truncated["stdout"],
                stderr_truncated=capture_truncated["stderr"],
                output_capture_truncated=any(capture_truncated.values()),
            )
            raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        _ACTIVE_PACKAGE_PROCESS = None
        signal.signal(signal.SIGTERM, previous_handlers[signal.SIGTERM])
        signal.signal(signal.SIGINT, previous_handlers[signal.SIGINT])

    cancel_signal = _PACKAGE_CANCEL_SIGNAL
    _PACKAGE_CANCEL_SIGNAL = None
    if cancel_signal is not None:
        _finish_package_history(
            root,
            history_record,
            "cancelled",
            package_exit_code,
            stdout=bytes(captures["stdout"]),
            stderr=bytes(captures["stderr"]),
            stdout_truncated=capture_truncated["stdout"],
            stderr_truncated=capture_truncated["stderr"],
            output_capture_truncated=any(capture_truncated.values()),
        )
        return 128 + cancel_signal

    os.write(1, f"Package manager exit code: {package_exit_code}\n".encode("ascii"))
    try:
        current_plan = prepare_plan(
            source_text,
            env=env,
            ide_root=ide_root,
            trusted_nearby=trusted_nearby,
        )
        inputs_changed = (
            current_plan["approval"] != plan["approval"]
            or target not in current_plan["tests"]
        )
        stale_reason = "package manifest, input tree, target, or package manager changed"
    except PackageTaskError as exc:
        inputs_changed = True
        stale_reason = f"package inputs no longer form a valid test plan: {exc}"
    if inputs_changed:
        message = f"Package test result is stale: {stale_reason}; it was not attached.\n"
        os.write(1, message.encode("utf-8", errors="replace"))
        result_code = STALE_SUCCESS_EXIT if package_exit_code == 0 else STALE_FAILURE_EXIT
        terminal_state = "stale"
    else:
        result_code = 0 if package_exit_code == 0 else 1
        terminal_state = "succeeded" if package_exit_code == 0 else "failed"
        stale_reason = None
    terminal_record = _finish_package_history(
        root,
        history_record,
        terminal_state,
        package_exit_code,
        stale_reason=stale_reason if inputs_changed else None,
        stdout=bytes(captures["stdout"]),
        stderr=bytes(captures["stderr"]),
        stdout_truncated=capture_truncated["stdout"],
        stderr_truncated=capture_truncated["stderr"],
        output_capture_truncated=any(capture_truncated.values()),
    )
    if terminal_record is not None:
        os.write(1, f"Task record updated: {terminal_record}\n".encode("utf-8", errors="replace"))
    return result_code


def main(arguments: list[str]) -> int:
    try:
        if len(arguments) == 3 and arguments[1] == "plan":
            plan = prepare_plan(arguments[2])
            sys.stdout.write(format_plan(plan))
            return 0
        if len(arguments) in (3, 4) and arguments[1] == "history":
            try:
                selection = int(arguments[3]) if len(arguments) == 4 else None
            except ValueError as exc:
                raise PackageTaskError("package test history index must be a decimal integer") from exc
            sys.stdout.write(format_package_history(arguments[2], selection))
            return 0
        if len(arguments) == 5 and arguments[1] == "test-explorer":
            try:
                selection = int(arguments[3])
                case_selection = int(arguments[4])
            except ValueError as exc:
                raise PackageTaskError("package test explorer indices must be decimal integers") from exc
            sys.stdout.write(format_package_test_explorer(arguments[2], selection, case_selection))
            return 0
        if len(arguments) == 5 and arguments[1] == "run":
            return run_test(arguments[2], arguments[3], arguments[4])
        sys.stderr.write("usage: package_runner.py plan SOURCE | run SOURCE TARGET APPROVAL | history SOURCE [INDEX] | test-explorer SOURCE INDEX CASE_INDEX\n")
        return 2
    except (PackageTaskError, OSError) as exc:
        sys.stderr.write(f"package test: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
