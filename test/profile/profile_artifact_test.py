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
        "summary": {},
        "quality": {"capture": "complete"},
        "source_mapping": {},
        "run": {},
        "functions": [],
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


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "capture.elisaprof"
        raw = write_artifact(path)
        result = ProfileArtifact.load(path)
        assert result.artifact_sha256 == hashlib.sha256(raw).hexdigest()
        assert result.identity.compiler_commit == "compiler-commit"
        assert result.identity.stage1_sha256 == "a" * 64
        assert result.identity.runtime_object_sha256 == "b" * 64
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

    print("ok profile artifact")


if __name__ == "__main__":
    main()
