"""Strict, bounded reader for Elisa profiler capture artifacts.

The profiler owns capture and rendering.  The IDE only accepts a complete,
versioned artifact after checking its envelope and retaining the compiler and
runtime identity that produced it.  In particular, this module does not fill
missing counters with zero: absent telemetry remains absent so the UI can
explain that a measurement was unavailable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ProfileError(ValueError):
    """The artifact is unreadable, malformed, too large, or incompatible."""


def _reject_constant(value: str) -> None:
    raise ProfileError(f"non-finite JSON number {value!r}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except ProfileError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProfileError(f"profile artifact is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError("profile artifact root must be a JSON object")
    return value


def _require(mapping: Mapping[str, Any], key: str, kind: type | tuple[type, ...] | None = None) -> Any:
    if key not in mapping:
        raise ProfileError(f"profile artifact is missing {key!r}")
    value = mapping[key]
    if kind is not None and not isinstance(value, kind):
        names = ", ".join(t.__name__ for t in kind) if isinstance(kind, tuple) else kind.__name__
        raise ProfileError(f"profile artifact field {key!r} must be {names}")
    return value


@dataclass(frozen=True)
class ProfileIdentity:
    """Build identity used to prevent stale profiling results being attached."""

    compiler_commit: str | None = None
    stage1_sha256: str | None = None
    runtime_object_sha256: str | None = None
    source_sha256: str | None = None
    edir_identity: str | None = None

    def mismatches(self, other: "ProfileIdentity") -> tuple[str, ...]:
        differences: list[str] = []
        for field in (
            "compiler_commit",
            "stage1_sha256",
            "runtime_object_sha256",
            "source_sha256",
            "edir_identity",
        ):
            expected = getattr(self, field)
            actual = getattr(other, field)
            if expected is not None and expected != actual:
                differences.append(field)
        return tuple(differences)

    def require_compatible(self, other: "ProfileIdentity") -> None:
        differences = self.mismatches(other)
        if differences:
            raise ProfileError("profile identity mismatch: " + ", ".join(differences))


@dataclass(frozen=True)
class ProfileArtifact:
    """A validated artifact and its unmodified embedded capture."""

    path: Path
    artifact: Mapping[str, Any]
    capture: Mapping[str, Any]
    identity: ProfileIdentity
    artifact_sha256: str
    raw_bytes: bytes

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        max_bytes: int = 128 << 20,
        expected_identity: ProfileIdentity | None = None,
    ) -> "ProfileArtifact":
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        artifact_path = Path(path)
        try:
            raw = artifact_path.read_bytes()
        except OSError as exc:
            raise ProfileError(f"could not read profile artifact {artifact_path}: {exc}") from exc
        if len(raw) > max_bytes:
            raise ProfileError(
                f"profile artifact is {len(raw)} bytes; limit is {max_bytes}"
            )
        artifact = _decode(raw)
        capture = cls._validate_envelope(artifact, len(raw), max_bytes)
        identity = cls._identity(capture)
        if expected_identity is not None:
            expected_identity.require_compatible(identity)
        return cls(
            path=artifact_path,
            artifact=artifact,
            capture=capture,
            identity=identity,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            raw_bytes=raw,
        )

    @staticmethod
    def _validate_envelope(
        artifact: Mapping[str, Any], raw_bytes: int, max_bytes: int
    ) -> Mapping[str, Any]:
        if _require(artifact, "artifact_version") != 1:
            raise ProfileError("unsupported profile artifact_version")
        if _require(artifact, "kind") != "elisa-profile":
            raise ProfileError("unsupported profile artifact kind")
        manifest = _require(artifact, "manifest", dict)
        if _require(manifest, "capture_format") != "profile-json-v2":
            raise ProfileError("unsupported profile capture format")
        if _require(manifest, "compression") != "none":
            raise ProfileError("compressed profile artifacts are not supported yet")
        capture_bytes = _require(manifest, "capture_bytes")
        if type(capture_bytes) is not int or capture_bytes <= 0:
            raise ProfileError("manifest.capture_bytes must be a positive integer")
        if capture_bytes > raw_bytes:
            raise ProfileError("manifest.capture_bytes exceeds the enclosing artifact")
        budget = manifest.get("max_artifact_bytes")
        if budget is not None and (type(budget) is not int or budget < raw_bytes):
            raise ProfileError("manifest.max_artifact_bytes is smaller than the artifact")
        if raw_bytes > max_bytes:
            raise ProfileError("profile artifact exceeds the configured limit")
        capture = _require(artifact, "capture", dict)
        if _require(capture, "schema_version") != 2:
            raise ProfileError("unsupported embedded profile schema_version")
        envelope = _require(capture, "envelope", dict)
        expected_envelope = {
            "major": 2,
            "minor": 0,
            "kind": "profile",
            "compatibility": "backward-compatible-v1",
        }
        if envelope != expected_envelope:
            raise ProfileError("embedded profile envelope is incompatible")
        for key in (
            "source",
            "compiler",
            "summary",
            "quality",
            "source_mapping",
            "run",
            "functions",
            "call_edges",
            "stacks",
            "locations",
        ):
            _require(capture, key)
        quality = _require(capture, "quality", dict)
        capture_state = _require(quality, "capture")
        if not isinstance(capture_state, str) or not capture_state:
            raise ProfileError("profile quality.capture must be a non-empty string")
        compiler = _require(capture, "compiler", dict)
        _require(compiler, "manifest")
        _require(compiler, "stage1_binary")
        for key in ("stage1_sha256", "runtime_object_sha256"):
            value = _require(compiler, key)
            if not isinstance(value, str) or len(value) != 64:
                raise ProfileError(f"compiler.{key} must be a SHA-256 hex string")
            try:
                int(value, 16)
            except ValueError as exc:
                raise ProfileError(f"compiler.{key} must be hexadecimal") from exc
        return capture

    @staticmethod
    def _identity(capture: Mapping[str, Any]) -> ProfileIdentity:
        compiler = capture["compiler"]
        source_snapshot = capture.get("source_snapshot")
        source_sha = None
        if isinstance(source_snapshot, dict) and isinstance(source_snapshot.get("sha256"), str):
            source_sha = source_snapshot["sha256"]
        return ProfileIdentity(
            compiler_commit=compiler.get("commit") if isinstance(compiler.get("commit"), str) else None,
            stage1_sha256=compiler.get("stage1_sha256"),
            runtime_object_sha256=compiler.get("runtime_object_sha256"),
            source_sha256=source_sha,
            edir_identity=(
                capture.get("target", {}).get("executable")
                if isinstance(capture.get("target"), dict)
                and isinstance(capture.get("target", {}).get("executable"), str)
                else None
            ),
        )
