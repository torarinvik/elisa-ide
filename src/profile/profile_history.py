"""Private atomic sidecar records for managed profile task history."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MAX_TASK_RECORD_BYTES = 64 << 10


class ProfileHistoryError(OSError):
    """A profile task history sidecar could not be persisted safely."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    try:
        raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProfileHistoryError(f"profile task history contains invalid data: {exc}") from exc
    if len(raw) > MAX_TASK_RECORD_BYTES:
        raise ProfileHistoryError(f"profile task history exceeds {MAX_TASK_RECORD_BYTES} bytes")
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
        raise ProfileHistoryError(f"could not save profile task history {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class ProfileTaskHistory:
    """One private, atomically replaced task sidecar beside its capture."""

    def __init__(self, path: Path, record: dict[str, Any]) -> None:
        self.path = path
        self._record = record

    @classmethod
    def start(
        cls,
        *,
        path: Path,
        source: Path,
        source_sha256: str,
        launch: Any,
        profiler_path: Path,
        profiler_selected_by: str,
        profiler_sha256: str,
        artifact_path: Path,
    ) -> "ProfileTaskHistory":
        record: dict[str, Any] = {
            "schema_version": 1,
            "task_id": artifact_path.stem,
            "state": "started",
            "started_at": _timestamp(),
            "finished_at": None,
            "source": {"path": os.fspath(source), "sha256": source_sha256},
            "launch": {
                "configuration_id": launch.configuration_id,
                "target": os.fspath(launch.target),
                "mode": launch.mode,
                "repetitions": launch.repetitions,
                "warmup": launch.warmup,
                "timeout_seconds": launch.timeout_seconds,
                "sample_period_us": launch.sample_period_us,
                "working_directory": os.fspath(launch.working_directory),
                "stdin": os.fspath(launch.stdin) if launch.stdin is not None else None,
                "arguments": list(launch.arguments),
                "environment_override_keys": [name for name, _value in launch.environment],
                "random_seed": launch.random_seed,
                "output_directory": (
                    os.fspath(launch.output_directory)
                    if launch.output_directory is not None else None
                ),
            },
            "profiler": {
                "path": os.fspath(profiler_path),
                "selected_by": profiler_selected_by,
                "sha256": profiler_sha256,
            },
            "artifact": {"path": os.fspath(artifact_path), "sha256": None},
            "compiler": None,
            "result": None,
            "error": None,
        }
        history = cls(path, record)
        _atomic_json(path, record)
        return history

    @classmethod
    def start_recovery(
        cls,
        *,
        path: Path,
        source: Path,
        source_sha256: str,
        manifest_path: Path,
        capture_path: Path,
        manifest: Mapping[str, Any],
        profiler_path: Path,
        profiler_selected_by: str,
        profiler_sha256: str,
        artifact_path: Path,
    ) -> "ProfileTaskHistory":
        """Record an offline recovery attempt without inventing launch inputs."""

        record: dict[str, Any] = {
            "schema_version": 1,
            "task_id": artifact_path.stem,
            "attempt": "recovery",
            "state": "started",
            "started_at": _timestamp(),
            "finished_at": None,
            "source": {"path": os.fspath(source), "sha256": source_sha256},
            "launch": {
                "configuration_id": "recovered-manifest",
                "target": os.fspath(source),
                "mode": manifest.get("collection_mode"),
                "repetitions": manifest.get("requested_repetitions"),
                "warmup": None,
                "timeout_seconds": None,
                "sample_period_us": manifest.get("sample_period_microseconds"),
                "working_directory": None,
                "stdin": None,
                "arguments": [],
                "environment_override_keys": [],
                "random_seed": None,
                "output_directory": None,
            },
            "profiler": {
                "path": os.fspath(profiler_path),
                "selected_by": profiler_selected_by,
                "sha256": profiler_sha256,
            },
            "artifact": {"path": os.fspath(artifact_path), "sha256": None},
            "recovery": {
                "manifest": os.fspath(manifest_path),
                "capture_path": os.fspath(capture_path),
                "manifest_sha256": manifest.get("_ide_manifest_sha256"),
                "state": manifest.get("state"),
                "valid_frames": (
                    manifest.get("capture_index", {}).get("valid_frames")
                    if isinstance(manifest.get("capture_index"), Mapping) else None
                ),
                "valid_bytes": (
                    manifest.get("capture_index", {}).get("valid_bytes")
                    if isinstance(manifest.get("capture_index"), Mapping) else None
                ),
            },
            "compiler": None,
            "result": None,
            "error": None,
        }
        history = cls(path, record)
        _atomic_json(path, record)
        return history

    def finish_success(self, result: Any) -> None:
        self._finish_result(result, "succeeded")

    def finish_recovered(self, result: Any) -> None:
        self._finish_result(result, "recovered")

    def _finish_result(self, result: Any, state: str) -> None:
        identity = result.identity
        summary = result.summary
        self._record.update({
            "state": state,
            "finished_at": _timestamp(),
            "artifact": {
                "path": os.fspath(result.path),
                "sha256": result.artifact_sha256,
            },
            "compiler": {
                "commit": identity.compiler_commit,
                "stage1_sha256": identity.stage1_sha256,
                "runtime_object_sha256": identity.runtime_object_sha256,
            },
            "result": {
                "capture_state": summary.capture_state,
                "outcome": summary.outcome,
                "collection_mode": summary.collection_mode,
                "event_count": summary.event_count,
                "requested_repetitions": summary.requested_repetitions,
                "completed_repetitions": summary.completed_repetitions,
                "capability_lines": list(summary.capability_lines),
            },
            "error": None,
        })
        _atomic_json(self.path, self._record)

    def finish_failure(self, *, state: str, error: str) -> None:
        if state not in {"failed", "cancelled"}:
            raise ValueError("profile task terminal state must be failed or cancelled")
        bounded_error = error.replace("\x00", "")[:2048]
        self._record.update({
            "state": state,
            "finished_at": _timestamp(),
            "error": bounded_error,
        })
        _atomic_json(self.path, self._record)
