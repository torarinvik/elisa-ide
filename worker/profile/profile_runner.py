#!/usr/bin/env python3
"""Run and validate one managed Elisa profiler capture.

The IDE owns this process. It launches the profiler as a separate process group
so Stop can terminate the collector, compiler, and profiled target together.
Only a successful, source-matching artifact is projected as a report; the
profiler's partial manifest and capture are left in place for its recovery
command when a job is interrupted.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE / "profile_lib", *_HERE.parents):
    if (_candidate / "src" / "profile" / "profile_artifact.py").is_file():
        sys.path.insert(0, os.fspath(_candidate))
        break

from src.profile.profile_artifact import ProfileArtifact, ProfileError, ProfileIdentity, _decode as _decode_profile_json  # noqa: E402
from src.profile.profile_config import (  # noqa: E402
    ProfileConfigError,
    ProfileLaunchConfig,
    load_profile_configuration,
)
from src.profile.profile_history import ProfileHistoryError, ProfileTaskHistory  # noqa: E402
from src.profile.profile_progress import ProfileProgress, ProfileProgressError  # noqa: E402
from src.toolchain.resolver import ToolchainError, resolve_compiler, resolve_tool  # noqa: E402


MAX_SOURCE_BYTES = 32 << 20
MAX_ARTIFACT_BYTES = 128 << 20
MAX_REPORT_BYTES = 64 << 20
MAX_MANIFEST_BYTES = 1 << 20
JOB_TIMEOUT_SECONDS = 30 * 60
REPORT_TIMEOUT_SECONDS = 120
VIEWER_TIMEOUT_SECONDS = 15
REPORT_FORMATS = {"html", "text", "folded", "speedscope"}
REPORT_EXTENSIONS = {
    "html": ".html",
    "text": ".txt",
    "folded": ".folded",
    "speedscope": ".speedscope.json",
}
_child: subprocess.Popen[bytes] | None = None
_termination_signal: int | None = None
_active_history: ProfileTaskHistory | None = None


class CaptureError(RuntimeError):
    """A profile request could not safely produce a current report."""


def _source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_SOURCE_BYTES:
                    raise CaptureError(
                        f"source file exceeds the {MAX_SOURCE_BYTES}-byte profile limit"
                    )
                digest.update(block)
    except CaptureError:
        raise
    except OSError as exc:
        raise CaptureError(f"could not read source file {path}: {exc}") from exc
    return digest.hexdigest()


def _project_root(source: Path) -> Path:
    for candidate in (source.parent, *source.parent.parents):
        if any(candidate.glob("*.elisaproject.json")):
            return candidate
    return source.parent


def _profile_directory(source: Path, root: Path, output_directory: Path | None = None) -> Path:
    configured = os.environ.get("ELISA_IDE_PROFILE_ROOT")
    if configured:
        base = Path(configured).expanduser()
        if not base.is_absolute():
            base = root / base
    elif output_directory is not None:
        base = output_directory
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Elisa IDE" / "Profiles"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "elisa-ide" / "profiles"
    return _profile_directory_at_base(root, base)


def _profile_directory_at_base(root: Path, base: Path) -> Path:
    project_key = hashlib.sha256(os.fsencode(root)).hexdigest()[:20]
    directory = base / project_key
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise CaptureError(f"could not create private profile directory {directory}: {exc}") from exc
    return directory


def _recovery_directories(
    source: Path, root: Path, output_directory: Path | None
) -> tuple[Path, ...]:
    configured = os.environ.get("ELISA_IDE_PROFILE_ROOT")
    if configured:
        bases = [Path(configured).expanduser()]
        if not bases[0].is_absolute():
            bases[0] = root / bases[0]
    else:
        if platform.system() == "Darwin":
            default_base = Path.home() / "Library" / "Application Support" / "Elisa IDE" / "Profiles"
        else:
            default_base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "elisa-ide" / "profiles"
        bases = [default_base]
        if output_directory is not None:
            bases.append(output_directory)
    directories: list[Path] = []
    for base in bases:
        candidate = _profile_directory_at_base(root, base)
        if candidate not in directories:
            directories.append(candidate)
    return tuple(directories)


def _recoverable_manifests(
    source: Path,
    source_digest: str,
    directories: tuple[Path, ...],
) -> list[tuple[int, Path, Path, dict[str, object]]]:
    """Return recent, source-matching partial manifests with private captures."""

    matches: list[tuple[int, Path, Path, dict[str, object]]] = []
    temporary_root = Path(tempfile.gettempdir()).resolve()
    for directory in directories:
        try:
            candidates = list(directory.glob("*.manifest.json"))
        except OSError:
            continue
        for manifest_path in candidates[:512]:
            try:
                info = manifest_path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    continue
                if stat.S_IMODE(info.st_mode) & 0o077:
                    continue
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    continue
                if info.st_size <= 0 or info.st_size > MAX_MANIFEST_BYTES:
                    continue
                payload = _decode_profile_json(manifest_path.read_bytes())
                if payload.get("kind") != "elisa_profile_capture_manifest" or payload.get("schema_version") != 1:
                    continue
                if payload.get("state") not in {"running", "partial", "finalizing"}:
                    continue
                if payload.get("source_sha256") != source_digest:
                    continue
                manifest_source = payload.get("source")
                capture_text = payload.get("capture_path")
                if not isinstance(manifest_source, str) or not isinstance(capture_text, str):
                    continue
                if Path(manifest_source).resolve(strict=True) != source:
                    continue
                capture_candidate = Path(capture_text)
                candidate_info = capture_candidate.lstat()
                if stat.S_ISLNK(candidate_info.st_mode) or not stat.S_ISREG(candidate_info.st_mode):
                    continue
                capture_path = capture_candidate.resolve(strict=True)
                capture_info = capture_path.stat()
                if capture_info.st_size <= 0 or capture_info.st_size > MAX_ARTIFACT_BYTES:
                    continue
                if stat.S_IMODE(capture_info.st_mode) & 0o077:
                    continue
                if hasattr(os, "getuid") and capture_info.st_uid != os.getuid():
                    continue
                capture_directory = capture_path.parent
                try:
                    capture_directory.relative_to(temporary_root)
                except ValueError:
                    continue
                directory_info = capture_directory.stat()
                if not capture_directory.name.startswith("elisa-profiler-native-"):
                    continue
                if stat.S_IMODE(directory_info.st_mode) & 0o077:
                    continue
                if hasattr(os, "getuid") and directory_info.st_uid != os.getuid():
                    continue
                payload["_ide_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                matches.append((info.st_mtime_ns, manifest_path, capture_path, payload))
            except (OSError, RuntimeError, ValueError, ProfileError):
                continue
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[:128]


def _artifact_path(source: Path, root: Path, output_directory: Path | None = None) -> Path:
    directory = _profile_directory(source, root, output_directory)
    stem = "".join(character if character.isalnum() or character in "-_" else "_" for character in source.stem)
    stem = stem[:64] or "source"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for _ in range(8):
        candidate = directory / f"{stem}-{timestamp}-{secrets.token_hex(4)}.elisaprof"
        if (
            not candidate.exists()
            and not candidate.with_name(candidate.name + ".manifest.json").exists()
            and not candidate.with_name(candidate.name + ".task.json").exists()
        ):
            return candidate
    raise CaptureError("could not allocate a unique profile artifact name")


def _ide_root() -> Path:
    for candidate in (_HERE, *_HERE.parents):
        if (candidate / "scripts" / "build_ide.sh").is_file():
            return candidate
    # In a packaged application the runner and its Python modules live under
    # Contents/Resources; installed toolchains are then resolved from the
    # user's explicit settings or PATH.
    return _HERE


def _resolve_profiler(ide_root: Path):
    configured = os.environ.get("ELISA_PROFILER", "").strip()
    explicit = None
    if configured:
        explicit = Path(configured).expanduser()
        if not explicit.is_absolute():
            explicit = Path.cwd() / explicit
    nearby = [
        ide_root.parent / "Elisa-profiler" / "bin" / "elisa-profiler",
        ide_root.parent / "elisa-profiler" / "bin" / "elisa-profiler",
    ]
    return resolve_tool(
        "elisa-profiler",
        ide_root,
        explicit=explicit,
        trusted_nearby=nearby,
        env_var=None,
        path_name="elisa-profiler",
        required=True,
    )


def _compiler_environment(ide_root: Path, env: dict[str, str]) -> None:
    configured_root = env.get("ELISA_COMPILER_ROOT") or env.get("ELISA_UI_STAGE1")
    candidate = Path(configured_root).expanduser() if configured_root else ide_root / "compiler"
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if not candidate.is_dir():
        # The profiler still has its own resolver for an installed toolchain.
        # When the IDE's paired checkout exists, however, always pass that pair
        # explicitly so its recorded compiler provenance is the one in use.
        return
    try:
        compiler = resolve_compiler(ide_root, compiler_root=candidate)
    except ToolchainError as exc:
        raise CaptureError(f"Elisa compiler/runtime is unavailable: {exc}") from exc
    env["ELISA_COMPILER_ROOT"] = os.fspath(compiler.root)
    env["ELISA_STAGE1_BIN"] = os.fspath(compiler.stage1.path)
    env["ELISA_RUNTIME_OBJ"] = os.fspath(compiler.runtime.path)


def _terminate_child(signum: int | None = None) -> None:
    global _child
    child = _child
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=0.15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=0.15)
        except subprocess.TimeoutExpired:
            pass


def _handle_signal(signum: int, _frame: object) -> None:
    global _termination_signal
    _termination_signal = signum
    _terminate_child(signum)


def _finish_active_history(
    *, state: str, error: str | None = None, result: ProfileArtifact | None = None
) -> None:
    global _active_history
    history = _active_history
    if history is None:
        return
    _active_history = None
    try:
        if result is not None:
            history.finish_success(result)
        else:
            history.finish_failure(state=state, error=error or state)
    except (OSError, ValueError) as exc:
        print(f"Profile history warning: {exc}", file=sys.stderr, flush=True)


def _finish_recovery_history(result: ProfileArtifact) -> None:
    global _active_history
    history = _active_history
    if history is None:
        return
    _active_history = None
    try:
        history.finish_recovered(result)
    except (OSError, ValueError) as exc:
        print(f"Profile history warning: {exc}", file=sys.stderr, flush=True)


def _poll_progress(path: Path, previous: ProfileProgress | None) -> ProfileProgress | None:
    try:
        current = ProfileProgress.read(path)
    except ProfileProgressError:
        # The profiler atomically replaces this file. Ignore a transient
        # truncated snapshot from older or third-party builds and retry next
        # frame; the completed artifact remains the authoritative result.
        return previous
    if current is not None and current != previous:
        print(current.render_line(), flush=True)
        return current
    return previous


def _saved_source(source_text: str) -> Path:
    source = Path(source_text).expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise CaptureError(f"source file does not exist: {source}: {exc}") from exc
    if not source.is_file() or source.suffix.casefold() != ".elisa":
        raise CaptureError("profiling requires a regular saved .elisa source file")
    return source


def _launch_output_directory(source: Path, root: Path) -> Path | None:
    try:
        return load_profile_configuration(source, root).output_directory
    except ProfileConfigError:
        return None


def _announce_recovery(source: Path, root: Path, source_digest: str, output_directory: Path | None) -> None:
    directories = _recovery_directories(source, root, output_directory)
    candidates = _recoverable_manifests(source, source_digest, directories)
    if candidates:
        print(
            f"Profile recovery available: {candidates[0][1]}; activate Recover to recover complete frames",
            flush=True,
        )


def _new_recovered_artifact_path(source: Path, directory: Path) -> Path:
    stem = "".join(character if character.isalnum() or character in "-_" else "_" for character in source.stem)
    stem = stem[:64] or "source"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for _ in range(8):
        candidate = directory / f"{stem}-{timestamp}-{secrets.token_hex(4)}.recovered.elisaprof"
        if not candidate.exists() and not candidate.with_name(candidate.name + ".task.json").exists():
            return candidate
    raise CaptureError("could not allocate a unique recovered profile artifact name")


def _atomic_private_write(path: Path, raw: bytes) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise CaptureError(f"could not save recovered profile artifact {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def recover_latest(source_text: str) -> int:
    """Recover the newest private partial capture for the unchanged source."""

    global _child, _active_history
    source = _saved_source(source_text)
    source_digest = _source_digest(source)
    root = _project_root(source)
    output_directory = _launch_output_directory(source, root)
    directories = _recovery_directories(source, root, output_directory)
    candidates = _recoverable_manifests(source, source_digest, directories)
    if not candidates:
        raise CaptureError("no private recoverable partial capture matches this saved source revision")
    _modified_ns, manifest_path, capture_path, manifest = candidates[0]

    ide_root = _ide_root()
    try:
        profiler = _resolve_profiler(ide_root)
    except ToolchainError as exc:
        raise CaptureError(f"Elisa profiler was not found: {exc}") from exc

    directory = manifest_path.parent
    artifact = _new_recovered_artifact_path(source, directory)
    history_path = artifact.with_name(artifact.name + ".task.json")
    raw_output_fd, raw_output_name = tempfile.mkstemp(
        prefix=artifact.stem + ".raw-", suffix=".json", dir=directory
    )
    os.close(raw_output_fd)
    raw_output = Path(raw_output_name)
    os.chmod(raw_output, 0o600)
    env = dict(os.environ)
    os.umask(0o077)

    try:
        _active_history = ProfileTaskHistory.start_recovery(
            path=history_path,
            source=source,
            source_sha256=source_digest,
            manifest_path=manifest_path,
            capture_path=capture_path,
            manifest=manifest,
            profiler_path=profiler.path,
            profiler_selected_by=profiler.selected_by,
            profiler_sha256=profiler.sha256,
            artifact_path=artifact,
        )
    except ProfileHistoryError as exc:
        raw_output.unlink(missing_ok=True)
        raise CaptureError(f"could not record profile recovery history: {exc}") from exc

    argv = [
        os.fspath(profiler.path),
        "recover",
        os.fspath(manifest_path),
        "--format",
        "json",
        "--output",
        os.fspath(raw_output),
    ]
    print(f"Recovering partial capture with {profiler.path.name}...", flush=True)
    try:
        _child = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _active_history.finish_failure(state="failed", error=str(exc))
        _active_history = None
        raw_output.unlink(missing_ok=True)
        raise CaptureError(f"could not start Elisa profiler recovery: {exc}") from exc

    try:
        exit_code = _finish_child(REPORT_TIMEOUT_SECONDS, "profiler recovery")
        if exit_code != 0:
            raise CaptureError(f"Elisa profiler recovery exited with status {exit_code}")
        if _source_digest(source) != source_digest:
            raise CaptureError("source changed during recovery; recovered frames were not attached")
        info = raw_output.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CaptureError("profiler recovery output is not a regular file")
        if info.st_size <= 0 or info.st_size > MAX_ARTIFACT_BYTES:
            raise CaptureError("profiler recovery output is outside the supported size range")
        recovered_capture = raw_output.read_bytes()
        prefix = (
            b'{"artifact_version":1,"kind":"elisa-profile","manifest":'
            b'{"capture_format":"profile-json-v2","compression":"none","capture_bytes":'
            + str(len(recovered_capture)).encode("ascii")
            + b',"max_artifact_bytes":'
            + str(MAX_ARTIFACT_BYTES).encode("ascii")
            + b'},"capture":'
        )
        wrapped = prefix + recovered_capture + b"}\n"
        if len(wrapped) > MAX_ARTIFACT_BYTES:
            raise CaptureError("recovered profile artifact exceeds the configured size limit")
        _atomic_private_write(artifact, wrapped)
        result = ProfileArtifact.load(
            artifact,
            max_bytes=MAX_ARTIFACT_BYTES,
            expected_identity=ProfileIdentity(source_sha256=source_digest),
        )
        if result.summary.capture_state != "recovered" or result.summary.outcome != "incomplete_artifact":
            raise CaptureError("profiler output did not describe a recovered partial artifact")
        recovered_source = result.capture.get("source")
        if not isinstance(recovered_source, str) or Path(recovered_source).resolve(strict=True) != source:
            raise CaptureError("recovered profile source path does not match the open source")
        recovery = result.capture.get("recovery")
        if not isinstance(recovery, dict) or recovery.get("source_sha256") != source_digest:
            raise CaptureError("recovered profile is missing the matching source digest")
        if _source_digest(source) != source_digest:
            artifact.unlink(missing_ok=True)
            raise CaptureError("source changed before recovery was attached")
    except (OSError, ProfileError) as exc:
        raise CaptureError(f"recovered profile output was rejected: {exc}") from exc
    finally:
        raw_output.unlink(missing_ok=True)

    _finish_recovery_history(result)
    print(result.summary.format_text(
        source=os.fspath(source),
        compiler_commit=result.identity.compiler_commit,
        artifact_path=os.fspath(artifact),
    ), flush=True)
    print(f"Profile task record: {history_path}", flush=True)
    print("Profile recovery accepted; compiler and runtime provenance remain unavailable", flush=True)
    print("Profile report available: activate Open Report to render and open it", flush=True)
    return 0


def _finish_child(timeout: int, description: str) -> int:
    """Wait for the currently-owned tool process and reap it."""

    global _child
    child = _child
    if child is None:
        raise CaptureError(f"{description} process was not started")
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_child()
        raise CaptureError(f"{description} exceeded its {timeout}-second deadline") from exc
    finally:
        _child = None
    if _termination_signal is not None:
        raise CaptureError(f"{description} was cancelled")
    return child.returncode


def _report_format() -> str:
    selected = os.environ.get("ELISA_IDE_PROFILE_REPORT_FORMAT", "html").strip().casefold()
    if selected not in REPORT_FORMATS:
        raise CaptureError(
            "ELISA_IDE_PROFILE_REPORT_FORMAT must be html, text, folded, or speedscope"
        )
    return selected


def _render_report(
    profiler: Path,
    artifact: Path,
    root: Path,
    env: dict[str, str],
) -> Path:
    global _child
    selected_format = _report_format()
    report = artifact.with_name(artifact.stem + REPORT_EXTENSIONS[selected_format])
    argv = [
        os.fspath(profiler),
        "report",
        os.fspath(artifact),
        "--format",
        selected_format,
        "--output",
        os.fspath(report),
    ]
    try:
        _child = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            # A renderer is a third-party tool. Discard diagnostics instead of
            # allowing an unbounded stderr stream to consume IDE memory.
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise CaptureError(f"could not start profiler report renderer: {exc}") from exc
    exit_code = _finish_child(REPORT_TIMEOUT_SECONDS, "profiler report renderer")
    if exit_code != 0:
        raise CaptureError(f"profiler could not render the saved capture (status {exit_code})")
    try:
        info = report.lstat()
        if not report.is_file() or report.is_symlink():
            raise CaptureError("profiler report output is not a regular file")
        if info.st_size <= 0 or info.st_size > MAX_REPORT_BYTES:
            raise CaptureError(
                f"profiler report size is outside the supported range 1..{MAX_REPORT_BYTES} bytes"
            )
        report.chmod(0o600)
    except OSError as exc:
        raise CaptureError(f"could not secure the generated profiler report: {exc}") from exc
    return report


def _resolve_viewer(ide_root: Path):
    configured = os.environ.get("ELISA_IDE_VIEWER", "").strip()
    explicit: Path | None = None
    path_name = "open" if platform.system() == "Darwin" else "xdg-open"
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_absolute() or os.sep in configured or (os.altsep and os.altsep in configured):
            explicit = configured_path
        else:
            path_name = configured
    return resolve_tool(
        "profile report viewer",
        ide_root,
        explicit=explicit,
        env_var=None,
        path_name=path_name,
        required=True,
    )


def open_latest_report(source_text: str) -> int:
    """Render and open the newest validated capture matching the saved source."""

    global _child
    source = _saved_source(source_text)
    source_digest = _source_digest(source)
    root = _project_root(source)
    try:
        launch = load_profile_configuration(source, root)
    except ProfileConfigError as exc:
        raise CaptureError(f"profile launch configuration is invalid: {exc}") from exc
    profile_directory = _profile_directory(source, root, launch.output_directory)
    candidates: list[tuple[int, Path]] = []
    try:
        for path in profile_directory.glob("*.elisaprof"):
            try:
                info = path.lstat()
            except OSError:
                continue
            if path.is_symlink() or not path.is_file() or info.st_size <= 0:
                continue
            candidates.append((info.st_mtime_ns, path))
    except OSError as exc:
        raise CaptureError(f"could not list saved profile captures: {exc}") from exc
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)

    selected: ProfileArtifact | None = None
    for _mtime_ns, artifact in candidates[:128]:
        try:
            result = ProfileArtifact.load(
                artifact,
                max_bytes=MAX_ARTIFACT_BYTES,
                expected_identity=ProfileIdentity(source_sha256=source_digest),
            )
        except (OSError, ProfileError):
            continue
        capture_source = result.capture.get("source")
        if isinstance(capture_source, str):
            try:
                if Path(capture_source).resolve(strict=True) == source:
                    selected = result
                    break
            except (OSError, RuntimeError):
                continue
    if selected is None:
        raise CaptureError("no validated profile capture matches this saved source revision")
    if _source_digest(source) != source_digest:
        raise CaptureError("source changed while selecting its profile report")

    ide_root = _ide_root()
    try:
        profiler = _resolve_profiler(ide_root)
    except ToolchainError as exc:
        raise CaptureError(f"Elisa profiler was not found: {exc}") from exc
    env = dict(os.environ)
    os.umask(0o077)
    report = _render_report(profiler.path, selected.path, root, env)
    if _source_digest(source) != source_digest:
        raise CaptureError("source changed while rendering its profile report")
    try:
        viewer = _resolve_viewer(ide_root)
    except ToolchainError as exc:
        raise CaptureError(f"profile report viewer is unavailable: {exc}") from exc

    if _source_digest(source) != source_digest:
        raise CaptureError("source changed before opening its profile report")
    try:
        _child = subprocess.Popen(
            [os.fspath(viewer.path), os.fspath(report)],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise CaptureError(f"could not start profile report viewer {viewer.path}: {exc}") from exc
    exit_code = _finish_child(VIEWER_TIMEOUT_SECONDS, "profile report viewer")
    if exit_code != 0:
        raise CaptureError(f"profile report viewer exited with status {exit_code}")
    print(f"Opened {report.suffix.lstrip('.')} profile report in {viewer.path.name}", flush=True)
    return 0


def run_capture(source_text: str) -> int:
    global _child, _active_history
    source = _saved_source(source_text)

    source_digest = _source_digest(source)
    root = _project_root(source)
    try:
        launch = load_profile_configuration(source, root)
    except ProfileConfigError as exc:
        raise CaptureError(f"profile launch configuration is invalid: {exc}") from exc
    ide_root = _ide_root()
    try:
        profiler = _resolve_profiler(ide_root)
    except ToolchainError as exc:
        raise CaptureError(f"Elisa profiler was not found: {exc}") from exc

    artifact = _artifact_path(source, root, launch.output_directory)
    progress = artifact.with_name(artifact.name + ".progress.json")
    env = dict(os.environ)
    _compiler_environment(ide_root, env)
    # Profile reports may retain source-derived details. Keep the artifact,
    # progress record, manifest, and profiler temporaries private by default.
    os.umask(0o077)
    env["ELISA_PROFILE_MAX_PROGRAM_OUTPUT_BYTES"] = env.get(
        "ELISA_PROFILE_MAX_PROGRAM_OUTPUT_BYTES", str(1 << 20)
    )
    history_path = artifact.with_name(artifact.name + ".task.json")
    try:
        _active_history = ProfileTaskHistory.start(
            path=history_path,
            source=source,
            source_sha256=source_digest,
            launch=launch,
            profiler_path=profiler.path,
            profiler_selected_by=profiler.selected_by,
            profiler_sha256=profiler.sha256,
            artifact_path=artifact,
        )
    except ProfileHistoryError as exc:
        raise CaptureError(f"could not record profile task history: {exc}") from exc

    argv = [
        os.fspath(profiler.path),
        "record",
        os.fspath(source),
        "--mode",
        launch.mode,
        "--format",
        "json",
        "--repeat",
        str(launch.repetitions),
        "--warmup",
        str(launch.warmup),
        "--timeout",
        str(launch.timeout_seconds),
        "--max-artifact-bytes",
        str(MAX_ARTIFACT_BYTES),
        "--no-cache",
        "--progress",
        os.fspath(progress),
        "--artifact-output",
        os.fspath(artifact),
    ]
    if launch.sample_period_us is not None:
        argv.extend(("--sample-period-us", str(launch.sample_period_us)))
    argv.extend(("--cwd", os.fspath(launch.working_directory)))
    if launch.stdin is not None:
        argv.extend(("--stdin", os.fspath(launch.stdin)))
    for name, value in launch.environment:
        argv.extend(("--env", f"{name}={value}"))
    if launch.random_seed is not None:
        argv.extend(("--random-seed", str(launch.random_seed)))
    if launch.arguments:
        argv.append("--")
        argv.extend(launch.arguments)
    print(
        f"Profile configuration: {launch.configuration_id}; mode={launch.mode}; "
        f"repetitions={launch.repetitions}; warmup={launch.warmup}; "
        f"target timeout={launch.timeout_seconds}s",
        flush=True,
    )
    print(f"Profiling saved source with {profiler.path.name} ({profiler.selected_by})...", flush=True)
    try:
        _child = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            start_new_session=True,
        )
    except OSError as exc:
        raise CaptureError(f"could not start Elisa profiler {profiler.path}: {exc}") from exc

    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    last_progress: ProfileProgress | None = None
    try:
        while True:
            try:
                exit_code = _child.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    _terminate_child()
                    raise CaptureError(
                        f"profiler exceeded its {JOB_TIMEOUT_SECONDS}-second job deadline; "
                        "the partial manifest was retained for recovery"
                    )
                if _termination_signal is not None:
                    _finish_active_history(state="cancelled", error="profile capture was cancelled")
                    _announce_recovery(source, root, source_digest, launch.output_directory)
                    return 128 + _termination_signal
                last_progress = _poll_progress(progress, last_progress)
        if _termination_signal is not None:
            _finish_active_history(state="cancelled", error="profile capture was cancelled")
            _announce_recovery(source, root, source_digest, launch.output_directory)
            return 128 + _termination_signal
    finally:
        _child = None

    last_progress = _poll_progress(progress, last_progress)

    if exit_code != 0:
        _announce_recovery(source, root, source_digest, launch.output_directory)
        raise CaptureError(
            f"Elisa profiler exited with status {exit_code}; inspect its output and the retained manifest"
        )
    if _source_digest(source) != source_digest:
        raise CaptureError("source changed during profiling; the capture is retained but not attached")

    try:
        result = ProfileArtifact.load(
            artifact,
            max_bytes=MAX_ARTIFACT_BYTES,
            expected_identity=ProfileIdentity(source_sha256=source_digest),
        )
    except ProfileError as exc:
        raise CaptureError(f"profiler output was rejected: {exc}; artifact retained at {artifact}") from exc

    if result.summary.collection_mode != launch.mode:
        raise CaptureError(
            f"profiler returned mode {result.summary.collection_mode or 'not recorded'}, "
            f"expected configured mode {launch.mode}; artifact retained at {artifact}"
        )
    if result.summary.requested_repetitions != launch.repetitions:
        raise CaptureError(
            f"profiler returned {result.summary.requested_repetitions!r} requested repetitions, "
            f"expected {launch.repetitions}; artifact retained at {artifact}"
        )

    try:
        artifact.chmod(0o600)
        manifest = artifact.with_name(artifact.name + ".manifest.json")
        if manifest.exists():
            manifest.chmod(0o600)
    except OSError as exc:
        raise CaptureError(f"could not secure completed profile artifact: {exc}") from exc

    compiler_commit = result.identity.compiler_commit
    summary = result.summary.format_text(
        source=os.fspath(source),
        compiler_commit=compiler_commit,
        artifact_path=os.fspath(artifact),
    )
    print(summary, flush=True)
    _finish_active_history(state="succeeded", result=result)
    print(f"Profile task record: {history_path}", flush=True)
    print("Profile report available: activate Open Report to render and open it", flush=True)
    print(f"Compiler stage1 SHA-256: {result.identity.stage1_sha256 or 'not recorded'}", flush=True)
    print(f"Compiler runtime SHA-256: {result.identity.runtime_object_sha256 or 'not recorded'}", flush=True)
    print(
        f"Artifact SHA-256: {result.artifact_sha256}; profiler SHA-256: {profiler.sha256}",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="saved Elisa source file to profile")
    parser.add_argument(
        "--open-latest-report",
        action="store_true",
        help="render the latest validated capture for this unchanged source and open it",
    )
    parser.add_argument(
        "--recover-latest",
        action="store_true",
        help="recover the newest private partial capture matching this unchanged source",
    )
    arguments = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        if arguments.open_latest_report:
            return open_latest_report(arguments.source)
        if arguments.recover_latest:
            return recover_latest(arguments.source)
        status = run_capture(arguments.source)
        if status != 0:
            _finish_active_history(state="cancelled", error="profile capture was cancelled")
        return status
    except CaptureError as exc:
        _finish_active_history(
            state="cancelled" if _termination_signal is not None else "failed",
            error=str(exc),
        )
        print(f"Profile failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
