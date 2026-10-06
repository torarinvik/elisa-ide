"""Bounded reader for the Elisa profiler's atomic progress snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_PROGRESS_BYTES = 16 * 1024
_STATES = {
    "compile_started",
    "compile_complete",
    "warmup_complete",
    "capture_started",
    "repetition_complete",
    "rendering",
    "complete",
}
_FIELDS = {
    "kind",
    "schema_version",
    "state",
    "repetition",
    "requested_repetitions",
    "elapsed_ns",
    "events",
    "capture_bytes",
    "valid_frames",
    "capture_started",
    "capture_complete",
    "sample_count",
    "sample_missed",
    "sample_period_microseconds",
    "sampling_setup_failed",
    "collector_status",
}
_REQUIRED_FIELDS = {
    "kind",
    "schema_version",
    "state",
    "repetition",
    "requested_repetitions",
    "elapsed_ns",
    "events",
    "capture_bytes",
    "valid_frames",
    "capture_started",
    "capture_complete",
}


class ProfileProgressError(ValueError):
    """A profiler progress snapshot is malformed or outside its bounds."""


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileProgressError(f"duplicate progress field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProfileProgressError(f"non-finite progress number {value!r}")


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProfileProgressError(f"progress field {field!r} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class ProfileProgress:
    """A validated snapshot. Optional sample counters remain unavailable when absent."""

    state: str
    repetition: int
    requested_repetitions: int
    elapsed_ns: int
    events: int
    capture_bytes: int
    valid_frames: int
    capture_started: bool
    capture_complete: bool
    sample_count: int | None
    sample_missed: int | None
    sample_period_microseconds: int | None
    sampling_setup_failed: int | None
    collector_status: int | None

    @classmethod
    def from_json(cls, raw: bytes) -> "ProfileProgress":
        if len(raw) > MAX_PROGRESS_BYTES:
            raise ProfileProgressError("progress snapshot exceeds its size limit")
        try:
            text = raw.decode("utf-8", errors="strict")
            value = json.loads(
                text,
                object_pairs_hook=_object_pairs,
                parse_constant=_reject_constant,
            )
        except ProfileProgressError:
            raise
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            # A reader may observe a non-atomic third-party write. The runner
            # treats this as a transient snapshot and retries on the next poll.
            raise ProfileProgressError(f"progress snapshot is not complete UTF-8 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ProfileProgressError("progress snapshot root must be an object")
        missing = _REQUIRED_FIELDS.difference(value)
        if missing:
            raise ProfileProgressError("progress snapshot is missing " + ", ".join(sorted(missing)))
        extra = set(value).difference(_FIELDS)
        if extra:
            raise ProfileProgressError("progress snapshot has unsupported fields: " + ", ".join(sorted(extra)))
        if value["kind"] != "elisa_profile_progress" or type(value["kind"]) is not str:
            raise ProfileProgressError("progress snapshot kind is not elisa_profile_progress")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ProfileProgressError("progress snapshot schema_version must be 1")
        state = value["state"]
        if type(state) is not str or state not in _STATES:
            raise ProfileProgressError("progress snapshot state is unsupported")

        repetition = _integer(value["repetition"], "repetition")
        requested = _integer(value["requested_repetitions"], "requested_repetitions", minimum=1)
        if repetition > requested:
            raise ProfileProgressError("progress repetition exceeds its requested repetition count")
        elapsed_ns = _integer(value["elapsed_ns"], "elapsed_ns")
        events = _integer(value["events"], "events")
        capture_bytes = _integer(value["capture_bytes"], "capture_bytes")
        valid_frames = _integer(value["valid_frames"], "valid_frames")
        for field in ("capture_started", "capture_complete"):
            if type(value[field]) is not bool:
                raise ProfileProgressError(f"progress field {field!r} must be a boolean")

        optional: dict[str, int | None] = {}
        for field in (
            "sample_count",
            "sample_missed",
            "sample_period_microseconds",
            "sampling_setup_failed",
            "collector_status",
        ):
            if field not in value:
                optional[field] = None
                continue
            number = _integer(value[field], field)
            if field == "sampling_setup_failed" and number not in (0, 1):
                raise ProfileProgressError("progress sampling_setup_failed must be 0 or 1")
            if field == "collector_status" and number not in (0, 1, 2, 3):
                raise ProfileProgressError("progress collector_status is outside the supported range")
            optional[field] = number

        return cls(
            state=state,
            repetition=repetition,
            requested_repetitions=requested,
            elapsed_ns=elapsed_ns,
            events=events,
            capture_bytes=capture_bytes,
            valid_frames=valid_frames,
            capture_started=value["capture_started"],
            capture_complete=value["capture_complete"],
            sample_count=optional["sample_count"],
            sample_missed=optional["sample_missed"],
            sample_period_microseconds=optional["sample_period_microseconds"],
            sampling_setup_failed=optional["sampling_setup_failed"],
            collector_status=optional["collector_status"],
        )

    @classmethod
    def read(cls, path: Path) -> "ProfileProgress | None":
        """Read a bounded snapshot; return None until the profiler creates it."""

        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_PROGRESS_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProfileProgressError(f"could not read progress snapshot: {exc}") from exc
        return cls.from_json(raw)

    def render_line(self) -> str:
        labels = {
            "compile_started": "compiling",
            "compile_complete": "compile complete",
            "warmup_complete": "warm-up complete",
            "capture_started": "capturing",
            "repetition_complete": "repetition complete",
            "rendering": "rendering report",
            "complete": "capture complete",
        }
        line = (
            f"Profile progress: {labels[self.state]} "
            f"(repetition {self.repetition}/{self.requested_repetitions}; "
            f"{self.events} events; {self.valid_frames} valid frames)"
        )
        if self.sample_count is not None:
            line += f"; {self.sample_count} samples"
            if self.sample_missed is not None:
                line += f", {self.sample_missed} missed"
        if self.sample_period_microseconds is not None:
            line += f"; sample period {self.sample_period_microseconds} µs"
        if self.sampling_setup_failed == 1:
            line += "; sampling setup failed"
        return line
