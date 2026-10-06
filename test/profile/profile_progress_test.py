from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(ROOT))
from src.profile.profile_progress import ProfileProgress, ProfileProgressError


def snapshot(**changes):
    value = {
        "kind": "elisa_profile_progress",
        "schema_version": 1,
        "state": "capture_started",
        "repetition": 1,
        "requested_repetitions": 3,
        "elapsed_ns": 17,
        "events": 11,
        "capture_bytes": 240,
        "valid_frames": 2,
        "capture_started": True,
        "capture_complete": False,
        "sample_count": 4,
        "sample_missed": 1,
        "sample_period_microseconds": 500,
        "sampling_setup_failed": 0,
        "collector_status": 0,
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def main() -> None:
    progress = ProfileProgress.from_json(snapshot())
    assert progress.render_line() == (
        "Profile progress: capturing (repetition 1/3; 11 events; 2 valid frames); "
        "4 samples, 1 missed; sample period 500 µs"
    )

    # Optional sampling details stay unavailable rather than becoming zero.
    sparse = json.loads(snapshot())
    for field in (
        "sample_count",
        "sample_missed",
        "sample_period_microseconds",
        "sampling_setup_failed",
        "collector_status",
    ):
        sparse.pop(field)
    sparse_progress = ProfileProgress.from_json(json.dumps(sparse).encode())
    assert sparse_progress.sample_count is None
    assert "samples" not in sparse_progress.render_line()

    for malformed in (
        b"{",
        snapshot(events=True),
        snapshot(repetition=4),
        snapshot(capture_complete=1),
        snapshot(state="finished"),
        snapshot(collector_status=4),
        snapshot(unexpected=True),
        snapshot()[:-1] + b',"events":12}',
    ):
        try:
            ProfileProgress.from_json(malformed)
        except ProfileProgressError:
            pass
        else:
            raise AssertionError(f"accepted invalid progress: {malformed!r}")

    with tempfile.TemporaryDirectory(prefix="elisa-progress-read-") as directory:
        path = Path(directory) / "progress.json"
        assert ProfileProgress.read(path) is None
        path.write_bytes(b"{")  # A poll can race a non-atomic replacement.
        try:
            ProfileProgress.read(path)
        except ProfileProgressError:
            pass
        else:
            raise AssertionError("accepted an interrupted progress write")
        replacement = Path(directory) / "progress.tmp"
        replacement.write_bytes(snapshot(state="complete", repetition=3, capture_complete=True))
        replacement.replace(path)
        assert ProfileProgress.read(path).state == "complete"

    print("ok profile progress")


if __name__ == "__main__":
    main()
