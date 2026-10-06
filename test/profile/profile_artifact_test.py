from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(ROOT))
from src.profile.profile_artifact import ProfileArtifact, ProfileError, ProfileIdentity


def capture() -> dict:
    return {
        "schema_version": 2,
        "envelope": {
            "major": 2,
            "minor": 0,
            "kind": "profile",
            "compatibility": "backward-compatible-v1",
        },
        "source": "main.elisa",
        "compiler": {
            "manifest": "compiler-manifest.json",
            "stage1_binary": "elisa-stage1",
            "stage1_sha256": "a" * 64,
            "runtime_object_sha256": "b" * 64,
            "commit": "compiler-commit",
        },
        "summary": {
            "events": 7,
            "locations": 3,
            "dropped": 0,
            "function_events": 4,
            "statement_events": 2,
            "value_events": 1,
        },
        "quality": {"capture": "complete"},
        "source_mapping": {},
        "workload": {"source_sha256": "c" * 64},
        "run": {
            "collection_mode": "functions",
            "outcome": "success",
            "execution_ms_mean": 2.5,
            "requested_repetitions": 2,
            "completed_repetitions": 2,
            "capabilities": {
                "event_classes": ["function"],
                "trace": "disabled",
                "recent_path": "last_256",
                "timing": "wall",
                "sampling": "disabled",
                "sampling_detail": {"status": "disabled", "reason": "mode_not_selected", "scope": "none"},
                "allocation": {"status": "disabled", "reason": "mode_not_selected", "scope": "none"},
                "tasks": {"status": "unsupported", "reason": "task_lifecycle_hooks_unavailable", "scope": "none"},
                "identity": {"status": "compiler_stable_ids", "reason": "compiler_issued_function_and_location_ids", "scope": "capture"},
            },
        },
        "functions": [
            {"function": "slow_path", "inclusive_ns": 40, "self_ns": 30, "completed_calls": 1},
            {"function": "fast_path", "inclusive_ns": 10, "self_ns": 10, "completed_calls": 4},
            {"function": "slow_path", "inclusive_ns": 15, "self_ns": 8, "completed_calls": 2},
        ],
        "call_edges": [],
        "stacks": [],
        "locations": [],
    }


def write_artifact(path: Path, embedded: dict | None = None) -> bytes:
    cap = embedded or capture()
    artifact = {
        "artifact_version": 1,
        "kind": "elisa-profile",
        "manifest": {
            "capture_format": "profile-json-v2",
            "compression": "none",
            "capture_bytes": 1,
            "max_artifact_bytes": 128 << 20,
        },
        "capture": cap,
    }
    raw = json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return raw


def recovered_capture() -> dict:
    result = capture()
    result["compiler"] = {
        "root": "unavailable-recovery",
        "branch": "unavailable-recovery",
        "manifest": "unavailable-recovery",
        "stage1_binary": "unavailable-recovery",
        "stage1_sha256": None,
        "runtime_object": "unavailable-recovery",
        "runtime_object_sha256": None,
        "commit": "unavailable-recovery",
    }
    result["quality"] = {"capture": "recovered", "reasons": ["recovered_partial"]}
    result["run"]["outcome"] = "incomplete_artifact"
    result["recovery"] = {
        "kind": "partial-capture",
        "state": "running",
        "manifest": "/private/capture.manifest.json",
        "capture_path": "/private/capture.txt",
        "source_sha256": "c" * 64,
        "capture_bytes": 100,
        "valid_frames": 2,
        "valid_bytes": 80,
        "tail_truncated": True,
    }
    return result


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "capture.elisaprof"
        raw = write_artifact(path)
        result = ProfileArtifact.load(path)
        assert result.artifact_sha256 == hashlib.sha256(raw).hexdigest()
        assert result.identity.compiler_commit == "compiler-commit"
        assert result.identity.stage1_sha256 == "a" * 64
        assert result.identity.runtime_object_sha256 == "b" * 64
        assert result.identity.source_sha256 == "c" * 64
        assert result.summary.execution_mean_ms == 2.5
        assert result.summary.event_count == 7
        assert result.summary.functions[0].name == "slow_path"
        assert result.summary.functions[0].inclusive_ns == 55
        assert result.summary.functions[0].completed_calls == 3
        report = result.summary.format_text(
            source="main.elisa", compiler_commit="compiler-commit", artifact_path="capture.elisaprof"
        )
        assert "statement; 1 value" in report
        assert "slow_path: 55 ns inclusive" in report
        assert "Retained event classes: function" in report
        assert "CPU sampling: disabled; scope none; reason mode_not_selected" in report
        assert "Allocation lifecycle: disabled; scope none; reason mode_not_selected" in report
        assert "Task lifecycle: unsupported; scope none; reason task_lifecycle_hooks_unavailable" in report
        assert "Native/foreign frame unwind: not collected" in report
        assert "Process attach: unavailable" in report

        write_artifact(path, recovered_capture())
        recovered = ProfileArtifact.load(
            path, expected_identity=ProfileIdentity(source_sha256="c" * 64)
        )
        assert recovered.summary.capture_state == "recovered"
        assert recovered.summary.outcome == "incomplete_artifact"
        assert recovered.summary.execution_mean_ms is None
        assert recovered.summary.requested_repetitions is None
        assert recovered.summary.completed_repetitions is None
        assert recovered.identity.compiler_commit is None
        assert recovered.identity.stage1_sha256 is None
        assert recovered.identity.runtime_object_sha256 is None
        recovered_report = recovered.summary.format_text(
            source="main.elisa", compiler_commit=None, artifact_path="recovered.elisaprof"
        )
        assert "Capture: recovered" in recovered_report
        assert "Compiler revision: not recorded" in recovered_report
        assert "Target execution mean: not recorded" in recovered_report
        assert "Repetitions: not recorded completed of not recorded requested" in recovered_report

        malformed_recovery = recovered_capture()
        malformed_recovery["compiler"]["stage1_sha256"] = "0" * 64
        write_artifact(path, malformed_recovery)
        try:
            ProfileArtifact.load(path)
        except ProfileError as exc:
            assert "must remain null" in str(exc)
        else:
            raise AssertionError("recovered artifact invented compiler identity")

        without_capabilities = capture()
        del without_capabilities["run"]["capabilities"]
        write_artifact(path, without_capabilities)
        no_capability_report = ProfileArtifact.load(path).summary.format_text(
            source="main.elisa", compiler_commit="compiler-commit", artifact_path="capture.elisaprof"
        )
        assert "Profiler capability matrix: not recorded" in no_capability_report
        assert "Allocation lifecycle: not recorded" not in no_capability_report

        partial_capabilities = capture()
        del partial_capabilities["run"]["capabilities"]["allocation"]
        write_artifact(path, partial_capabilities)
        partial_report = ProfileArtifact.load(path).summary.format_text(
            source="main.elisa", compiler_commit="compiler-commit", artifact_path="capture.elisaprof"
        )
        assert "Allocation lifecycle: not recorded" in partial_report

        malformed_capabilities = capture()
        malformed_capabilities["run"]["capabilities"]["sampling_detail"] = {
            "status": "active\nforged", "reason": "x\nInjected line", "scope": "all_processes"
        }
        write_artifact(path, malformed_capabilities)
        sanitized_report = ProfileArtifact.load(path).summary.format_text(
            source="main.elisa", compiler_commit="compiler-commit", artifact_path="capture.elisaprof"
        )
        assert "CPU sampling: not recorded" in sanitized_report
        assert "Injected line" not in sanitized_report
        ProfileArtifact.load(
            path,
            expected_identity=ProfileIdentity(
                compiler_commit="compiler-commit",
                stage1_sha256="a" * 64,
            ),
        )
        try:
            ProfileArtifact.load(
                path,
                expected_identity=ProfileIdentity(compiler_commit="other"),
            )
        except ProfileError as exc:
            assert "compiler_commit" in str(exc)
        else:
            raise AssertionError("stale compiler identity was accepted")

        try:
            ProfileArtifact.load(
                path,
                expected_identity=ProfileIdentity(source_sha256="d" * 64),
            )
        except ProfileError as exc:
            assert "source_sha256" in str(exc)
        else:
            raise AssertionError("stale source identity was accepted")

        malformed = bytearray(raw.replace(b'"capture_format":"profile-json-v2"', b'"capture_format":"wrong"'))
        path.write_bytes(malformed)
        try:
            ProfileArtifact.load(path)
        except ProfileError:
            pass
        else:
            raise AssertionError("malformed capture format was accepted")

        path.write_bytes(b'{"kind":"elisa-profile","kind":"duplicate"}')
        try:
            ProfileArtifact.load(path)
        except ProfileError as exc:
            assert "duplicate" in str(exc)
        else:
            raise AssertionError("duplicate JSON key was accepted")

        path.write_bytes(raw)
        try:
            ProfileArtifact.load(path, max_bytes=len(raw) - 1)
        except ProfileError:
            pass
        else:
            raise AssertionError("oversized artifact was accepted")

        mismatched_snapshot = capture()
        mismatched_snapshot["source_snapshot"] = {"sha256": "d" * 64}
        path.write_bytes(write_artifact(path, mismatched_snapshot))
        try:
            ProfileArtifact.load(path)
        except ProfileError as exc:
            assert "does not match workload identity" in str(exc)
        else:
            raise AssertionError("inconsistent embedded and workload source digests were accepted")

    print("ok profile artifact")


if __name__ == "__main__":
    main()
