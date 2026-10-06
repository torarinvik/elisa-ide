#!/usr/bin/env python3
"""Deterministic profiler adapter fixture for the native shell integration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


arguments = sys.argv[1:]
if arguments and arguments[0] == "recover":
    manifest_path = Path(arguments[1])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = Path(manifest["source"]).resolve(strict=True)
    capture_path = Path(manifest["capture_path"])
    output_path = Path(arguments[arguments.index("--output") + 1])
    recovery = {
        "kind": "partial-capture",
        "state": manifest["state"],
        "manifest": str(manifest_path),
        "capture_path": str(capture_path),
        "source_sha256": manifest["source_sha256"],
        "capture_bytes": manifest["capture_index"]["bytes"],
        "valid_frames": manifest["capture_index"]["valid_frames"],
        "valid_bytes": manifest["capture_index"]["valid_bytes"],
        "tail_truncated": True,
    }
    recovered = {
        "schema_version": 2,
        "envelope": {"major": 2, "minor": 0, "kind": "profile", "compatibility": "backward-compatible-v1"},
        "source": str(source),
        "compiler": {
            "root": "unavailable-recovery", "branch": "unavailable-recovery",
            "commit": "unavailable-recovery", "dirty": True,
            "manifest": "unavailable-recovery", "stage1_binary": "unavailable-recovery",
            "stage1_sha256": None, "runtime_object": "unavailable-recovery",
            "runtime_object_sha256": None,
        },
        "summary": {"events": 0, "locations": 0, "dropped": 0},
        "quality": {"capture": "recovered", "detail": "degraded", "reasons": ["recovered_partial"]},
        "source_mapping": {},
        "workload": {"source_sha256": manifest["source_sha256"]},
        "run": {
            "collection_mode": manifest["collection_mode"],
            "outcome": "incomplete_artifact",
            "requested_repetitions": 1,
            "completed_repetitions": 1,
        },
        "functions": [], "call_edges": [], "stacks": [], "locations": [],
        "recovery": recovery,
    }
    output_path.write_text(json.dumps(recovered, separators=(",", ":")), encoding="utf-8")
    raise SystemExit(0)
if arguments and arguments[0] == "report":
    report_path = Path(arguments[arguments.index("--output") + 1])
    report_format = arguments[arguments.index("--format") + 1]
    report_path.write_text(
        f"<!doctype html><title>Elisa profile fixture ({report_format})</title>\n",
        encoding="utf-8",
    )
    raise SystemExit(0)
if not arguments or arguments[0] != "record":
    raise SystemExit("fake profiler only accepts record or report")
source = Path(arguments[1]).resolve(strict=True)
output = Path(arguments[arguments.index("--artifact-output") + 1])
progress_path = Path(arguments[arguments.index("--progress") + 1])
source_bytes = source.read_bytes()
source_digest = hashlib.sha256(source_bytes).hexdigest()

if os.environ.get("FAKE_PROFILE_SLEEP"):
    temporary = Path(tempfile.mkdtemp(prefix="elisa-profiler-native-shell-test-"))
    capture_path = temporary / "capture.txt"
    capture_path.write_bytes(b"ELISA_PROFILE partial shell fixture\n")
    capture_path.chmod(0o600)
    manifest_path = Path(str(output) + ".manifest.json")
    manifest = {
        "kind": "elisa_profile_capture_manifest", "schema_version": 1,
        "state": "running", "source": str(source), "source_sha256": source_digest,
        "collection_mode": "functions", "capture_path": str(capture_path),
        "requested_repetitions": 1, "completed_repetitions": 0,
        "successful_repetitions": 0, "failed_repetitions": 0, "events": 0,
        "capture_started": True, "capture_complete": False,
        "capture_index": {"format": "record-framed-v1", "bytes": 40, "valid_frames": 1, "valid_bytes": 32},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    started_marker = os.environ.get("FAKE_PROFILE_STARTED_FILE")
    if started_marker:
        Path(started_marker).write_text("started\n", encoding="utf-8")
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(60)

capture = {
    "schema_version": 2,
    "envelope": {
        "major": 2,
        "minor": 0,
        "kind": "profile",
        "compatibility": "backward-compatible-v1",
    },
    "source": str(source),
    "compiler": {
        "manifest": "compiler-manifest.json",
        "stage1_binary": "elisac-stage1",
        "stage1_sha256": "a" * 64,
        "runtime_object_sha256": "b" * 64,
        "commit": "shell-integration-fixture",
    },
    "summary": {
        "events": 3,
        "locations": 1,
        "dropped": 0,
        "function_events": 3,
    },
    "quality": {"capture": "complete", "detail": "complete", "reasons": []},
    "source_mapping": {},
    "workload": {"source_sha256": source_digest},
    "run": {
        "collection_mode": "functions",
        "outcome": "success",
        "execution_ms_mean": 1.25,
        "requested_repetitions": 1,
        "completed_repetitions": 1,
        "capabilities": {
            "event_classes": ["function"],
            "trace": "disabled",
            "recent_path": "last_256",
            "timing": "wall",
            "sampling": "disabled",
            "sampling_detail": {"status": "disabled", "reason": "mode_not_selected", "scope": "none"},
            "allocation": {"status": "disabled", "reason": "mode_not_selected", "scope": "none"},
            "tasks": {"status": "unsupported", "reason": "task_lifecycle_hooks_unavailable", "scope": "none"},
            "identity": {
                "status": "compiler_stable_ids",
                "reason": "compiler_issued_function_and_location_ids",
                "scope": "capture",
            },
        },
    },
    "functions": [
        {"function": "main", "inclusive_ns": 1000, "self_ns": 700, "completed_calls": 1}
    ],
    "call_edges": [],
    "stacks": [],
    "locations": [{"function": "main", "line": 1, "count": 1}],
}
artifact = {
    "artifact_version": 1,
    "kind": "elisa-profile",
    "manifest": {
        "capture_format": "profile-json-v2",
        "compression": "none",
        "capture_bytes": 1,
    },
    "capture": capture,
}
# Model the real profiler's atomic progress-file replacement. The runner must
# read the completed snapshot even when a short capture exits between polls.
progress = {
    "kind": "elisa_profile_progress",
    "schema_version": 1,
    "state": "complete",
    "repetition": 1,
    "requested_repetitions": 1,
    "elapsed_ns": 1_250_000,
    "events": 3,
    "capture_bytes": 128,
    "valid_frames": 1,
    "capture_started": True,
    "capture_complete": True,
    "sample_count": 0,
    "sample_missed": 0,
    "sample_period_microseconds": 0,
    "sampling_setup_failed": 0,
    "collector_status": 0,
}
progress_tmp = progress_path.with_name(progress_path.name + ".tmp")
progress_tmp.write_text(json.dumps(progress, separators=(",", ":")), encoding="utf-8")
os.replace(progress_tmp, progress_path)
output.write_text(json.dumps(artifact, separators=(",", ":")), encoding="utf-8")
print("fake profiler emitted a source-identified capture to stdout; runner should discard this")
