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
import math
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
class FunctionHotspot:
    """A bounded, aggregated function row for the initial report view."""

    name: str
    inclusive_ns: int | None
    self_ns: int | None
    completed_calls: int | None


@dataclass(frozen=True)
class ProfileSummary:
    """Validated summary facts; missing profiler telemetry stays unavailable."""

    capture_state: str
    detail_state: str | None
    outcome: str | None
    collection_mode: str | None
    event_count: int | None
    function_event_count: int | None
    statement_event_count: int | None
    value_event_count: int | None
    location_count: int | None
    dropped_event_count: int | None
    execution_mean_ms: float | None
    requested_repetitions: int | None
    completed_repetitions: int | None
    capability_lines: tuple[str, ...]
    functions: tuple[FunctionHotspot, ...]
    warnings: tuple[str, ...]

    @staticmethod
    def _capability_atom(value: Any) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 96:
            return None
        if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in value):
            return None
        return value

    @classmethod
    def _capability_lines(cls, run: Mapping[str, Any]) -> tuple[str, ...]:
        matrix = run.get("capabilities")
        if not isinstance(matrix, Mapping):
            return ("Profiler capability matrix: not recorded",)

        lines: list[str] = []
        event_classes = matrix.get("event_classes")
        if isinstance(event_classes, list):
            known = {"function", "statement", "value", "sample"}
            selected = [item for item in event_classes[:8] if isinstance(item, str) and item in known]
            lines.append(
                "Retained event classes: " + ", ".join(selected)
                if selected else "Retained event classes: not recorded"
            )
        else:
            lines.append("Retained event classes: not recorded")

        def detail_line(key: str, label: str) -> str:
            detail = matrix.get(key)
            if not isinstance(detail, Mapping):
                return f"{label}: not recorded"
            status = cls._capability_atom(detail.get("status"))
            if status not in {
                "active", "disabled", "unsupported", "unconfirmed",
                "source_name_fallback", "compiler_stable_ids",
            }:
                return f"{label}: not recorded"
            scope = cls._capability_atom(detail.get("scope"))
            if scope not in {"none", "capture", "instrumented_call_stack"}:
                scope = "not recorded"
            reason = cls._capability_atom(detail.get("reason"))
            reason_text = f"; reason {reason}" if reason is not None else ""
            return f"{label}: {status}; scope {scope}{reason_text}"

        for key, label in (
            ("sampling_detail", "CPU sampling"),
            ("allocation", "Allocation lifecycle"),
            ("tasks", "Task lifecycle"),
            ("identity", "Source identity"),
        ):
            lines.append(detail_line(key, label))

        for key, label, choices in (
            ("timing", "Timing basis", {"wall", "cpu"}),
            ("trace", "Event trace", {"full", "disabled"}),
            ("recent_path", "Recent path context", {"last_256", "disabled"}),
        ):
            value = matrix.get(key)
            lines.append(f"{label}: {value}" if isinstance(value, str) and value in choices else f"{label}: not recorded")
        return tuple(lines)

    @staticmethod
    def _count(value: Any) -> int | None:
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 40:
            return int(value)
        return None

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        if type(value) not in (int, float):
            return None
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None

    @classmethod
    def from_capture(cls, capture: Mapping[str, Any]) -> "ProfileSummary":
        quality = capture["quality"]
        run = capture["run"]
        counters = capture["summary"]
        capture_state = quality["capture"]
        detail_state = quality.get("detail") if isinstance(quality.get("detail"), str) else None
        outcome = run.get("outcome") if isinstance(run.get("outcome"), str) else None
        collection_mode = run.get("collection_mode") if isinstance(run.get("collection_mode"), str) else None
        execution_mean = cls._finite_number(
            run.get("execution_ms_mean", run.get("execution_ms"))
        )
        if capture_state == "recovered":
            # Offline recovery did not execute the workload. The profiler's
            # recovery serializer can emit zero-valued defaults for these
            # fields, so keep them unavailable in the IDE report.
            execution_mean = None

        grouped: dict[str, dict[str, Any]] = {}
        for entry in capture["functions"]:
            name = entry.get("function")
            if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 4096:
                continue
            aggregate = grouped.setdefault(
                name,
                {"inclusive_ns": 0, "self_ns": 0, "completed_calls": 0,
                 "seen_inclusive": False, "seen_self": False, "seen_calls": False},
            )
            for metric, seen_field in (
                ("inclusive_ns", "seen_inclusive"),
                ("self_ns", "seen_self"),
                ("completed_calls", "seen_calls"),
            ):
                number = cls._count(entry.get(metric))
                if number is not None:
                    aggregate[metric] += number
                    aggregate[seen_field] = True

        hotspots = [
            FunctionHotspot(
                name=name,
                inclusive_ns=values["inclusive_ns"] if values["seen_inclusive"] else None,
                self_ns=values["self_ns"] if values["seen_self"] else None,
                completed_calls=values["completed_calls"] if values["seen_calls"] else None,
            )
            for name, values in grouped.items()
        ]
        hotspots.sort(
            key=lambda item: (
                -(item.inclusive_ns if item.inclusive_ns is not None else item.self_ns if item.self_ns is not None else -1),
                item.name.casefold(),
                item.name,
            )
        )

        warnings: list[str] = []
        reasons = quality.get("reasons")
        if isinstance(reasons, list):
            warnings.extend(item for item in reasons if isinstance(item, str) and item)
        if capture_state != "complete":
            warnings.append(f"capture quality is {capture_state}")
        if detail_state is not None and detail_state != "complete":
            warnings.append(f"detail quality is {detail_state}")
        if outcome is not None and outcome != "success":
            warnings.append(f"profiled program outcome is {outcome}")
        dropped = cls._count(counters.get("dropped"))
        if dropped:
            warnings.append(f"profiler dropped {dropped} events")

        return cls(
            capture_state=capture_state,
            detail_state=detail_state,
            outcome=outcome,
            collection_mode=collection_mode,
            event_count=cls._count(counters.get("events")),
            function_event_count=cls._count(counters.get("function_events")),
            statement_event_count=cls._count(counters.get("statement_events")),
            value_event_count=cls._count(counters.get("value_events")),
            location_count=cls._count(counters.get("locations")),
            dropped_event_count=dropped,
            execution_mean_ms=execution_mean,
            requested_repetitions=(
                None if capture_state == "recovered" else cls._count(run.get("requested_repetitions"))
            ),
            completed_repetitions=(
                None if capture_state == "recovered" else cls._count(run.get("completed_repetitions"))
            ),
            capability_lines=cls._capability_lines(run),
            functions=tuple(hotspots[:8]),
            warnings=tuple(warnings[:32]),
        )

    def format_text(self, *, source: str, compiler_commit: str | None, artifact_path: str) -> str:
        """Render a concise IDE report without inventing absent measurements."""

        def count(value: int | None, label: str = "not recorded") -> str:
            return str(value) if value is not None else label

        lines = [
            "Profile capture accepted",
            f"Source: {source}",
            f"Capture: {self.capture_state}; mode: {self.collection_mode or 'not recorded'}; outcome: {self.outcome or 'not recorded'}",
            f"Events: {count(self.event_count)} total; {count(self.function_event_count)} function; {count(self.statement_event_count, 'not collected')} statement; {count(self.value_event_count, 'not collected')} value",
            f"Functions: {len(self.functions)} reported in top-function view; locations: {count(self.location_count)}; dropped events: {count(self.dropped_event_count)}",
            f"Target execution mean: {self.execution_mean_ms:.3f} ms" if self.execution_mean_ms is not None else "Target execution mean: not recorded",
            f"Repetitions: {count(self.completed_repetitions)} completed of {count(self.requested_repetitions)} requested",
            f"Compiler revision: {compiler_commit or 'not recorded'}",
            "Telemetry coverage:",
            *(f"  {line}" for line in self.capability_lines),
            "  Native/foreign frame unwind: not collected by this instrumented Elisa capture",
            "  Process attach: unavailable; this IDE action profiles a newly launched source process",
            "Top functions by inclusive time:",
        ]
        if self.functions:
            for item in self.functions:
                inclusive = f"{item.inclusive_ns} ns inclusive" if item.inclusive_ns is not None else "inclusive time not recorded"
                self_time = f"{item.self_ns} ns self" if item.self_ns is not None else "self time not recorded"
                calls = f"{item.completed_calls} completed calls" if item.completed_calls is not None else "calls not recorded"
                lines.append(f"  {item.name}: {inclusive}, {self_time}, {calls}")
        else:
            lines.append("  Function timing was not reported")
        if self.warnings:
            lines.append("Capture notes:")
            lines.extend(f"  {warning}" for warning in self.warnings)
        lines.append(f"Artifact: {artifact_path}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ProfileArtifact:
    """A validated artifact and its unmodified embedded capture."""

    path: Path
    artifact: Mapping[str, Any]
    capture: Mapping[str, Any]
    identity: ProfileIdentity
    summary: ProfileSummary
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
            summary=ProfileSummary.from_capture(capture),
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
        _require(capture, "summary", dict)
        _require(capture, "run", dict)
        _require(capture, "functions", list)
        _require(capture, "call_edges", list)
        _require(capture, "stacks", list)
        _require(capture, "locations", list)
        quality = _require(capture, "quality", dict)
        capture_state = _require(quality, "capture")
        if not isinstance(capture_state, str) or not capture_state:
            raise ProfileError("profile quality.capture must be a non-empty string")
        recovered = capture_state == "recovered"
        recovery = capture.get("recovery")
        if recovered:
            recovery = _require(capture, "recovery", dict)
            if _require(recovery, "kind") != "partial-capture":
                raise ProfileError("recovered profile is missing partial-capture provenance")
            if _require(recovery, "state") not in {"running", "partial", "finalizing"}:
                raise ProfileError("recovered profile has an unsupported manifest state")
            for key in ("manifest", "capture_path"):
                value = _require(recovery, key)
                if not isinstance(value, str) or not value or "\x00" in value:
                    raise ProfileError(f"recovery.{key} must be a non-empty path")
            recovery_digest = _require(recovery, "source_sha256")
            if not isinstance(recovery_digest, str) or len(recovery_digest) != 64 or any(
                character not in "0123456789abcdef" for character in recovery_digest
            ):
                raise ProfileError("recovery.source_sha256 must be a lowercase SHA-256 digest")
            for key in ("capture_bytes", "valid_frames", "valid_bytes"):
                value = _require(recovery, key)
                if type(value) is not int or value < 0:
                    raise ProfileError(f"recovery.{key} must be a non-negative integer")
            if type(_require(recovery, "tail_truncated")) is not bool:
                raise ProfileError("recovery.tail_truncated must be a boolean")
        compiler = _require(capture, "compiler", dict)
        _require(compiler, "manifest")
        _require(compiler, "stage1_binary")
        if recovered:
            for key in (
                "root", "branch", "commit", "manifest", "stage1_binary", "runtime_object",
            ):
                if _require(compiler, key) != "unavailable-recovery":
                    raise ProfileError(
                        f"recovered compiler.{key} must remain explicitly unavailable"
                    )
            for key in ("stage1_sha256", "runtime_object_sha256"):
                if _require(compiler, key) is not None:
                    raise ProfileError(f"recovered compiler.{key} must remain null")
        else:
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
        workload = capture.get("workload")
        source_sha = (
            source_snapshot.get("sha256")
            if isinstance(source_snapshot, dict) and isinstance(source_snapshot.get("sha256"), str)
            else workload.get("source_sha256")
            if isinstance(workload, dict) and isinstance(workload.get("source_sha256"), str)
            else None
        )
        workload_sha = workload.get("source_sha256") if isinstance(workload, dict) else None
        snapshot_sha = source_snapshot.get("sha256") if isinstance(source_snapshot, dict) else None
        for candidate in (source_sha, workload_sha, snapshot_sha):
            if candidate is not None:
                if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
                    raise ProfileError("profile source SHA-256 identity is malformed")
        if workload_sha is not None and snapshot_sha is not None and workload_sha != snapshot_sha:
            raise ProfileError("profile source snapshot digest does not match workload identity")
        recovered = capture.get("quality", {}).get("capture") == "recovered"
        recovery = capture.get("recovery") if recovered else None
        if recovered and isinstance(recovery, dict) and recovery.get("source_sha256") != source_sha:
            raise ProfileError("recovery source digest does not match workload identity")
        return ProfileIdentity(
            compiler_commit=(
                compiler.get("commit")
                if isinstance(compiler.get("commit"), str) and compiler.get("commit") != "unavailable-recovery"
                else None
            ),
            stage1_sha256=(None if recovered else compiler.get("stage1_sha256")),
            runtime_object_sha256=(None if recovered else compiler.get("runtime_object_sha256")),
            source_sha256=source_sha,
            edir_identity=(
                capture.get("target", {}).get("executable")
                if isinstance(capture.get("target"), dict)
                and isinstance(capture.get("target", {}).get("executable"), str)
                else None
            ),
        )
