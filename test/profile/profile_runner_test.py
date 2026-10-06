from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "worker" / "profile" / "profile_runner.py"


FAKE_PROFILER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import hashlib, json, os, subprocess, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    if args and args[0] == "recover":
        if os.environ.get("FAKE_PROFILE_ARGS_LOG"):
            Path(os.environ["FAKE_PROFILE_ARGS_LOG"]).write_text(json.dumps(args), encoding="utf-8")
        output = Path(args[args.index("--output") + 1])
        if os.environ.get("FAKE_RECOVERY_OUTPUT_MODE"):
            Path(os.environ["FAKE_RECOVERY_OUTPUT_MODE"]).write_text(oct(output.stat().st_mode & 0o777), encoding="utf-8")
        output.write_bytes(Path(os.environ["FAKE_RECOVERY_CAPTURE"]).read_bytes())
        raise SystemExit(0)
    if args and args[0] == "report":
        report = Path(args[args.index("--output") + 1])
        report_format = args[args.index("--format") + 1]
        if os.environ.get("FAKE_REPORT_FAIL"):
            raise SystemExit(3)
        report.write_text("<!doctype html><title>Elisa profile fixture</title>\\n", encoding="utf-8")
        raise SystemExit(0)
    if not args or args[0] != "record":
        raise SystemExit("fake profiler only accepts record or report")
    if os.environ.get("FAKE_PROFILE_ARGS_LOG"):
        Path(os.environ["FAKE_PROFILE_ARGS_LOG"]).write_text(json.dumps(args), encoding="utf-8")
    source = Path(args[args.index("record") + 1])
    collection_mode = args[args.index("--mode") + 1]
    requested_repetitions = int(args[args.index("--repeat") + 1])
    output = Path(args[args.index("--artifact-output") + 1])
    raw_source = source.read_bytes()
    digest = hashlib.sha256(raw_source).hexdigest()
    if os.environ.get("FAKE_PROFILE_SLEEP"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(os.environ["FAKE_PROFILE_CHILD_PID_FILE"]).write_text(str(child.pid))
        time.sleep(60)
    if os.environ.get("FAKE_PROFILE_PROGRESS"):
        progress = Path(args[args.index("--progress") + 1])
        progress.write_text("{", encoding="utf-8")
        time.sleep(0.32)
        state = {
            "kind": "elisa_profile_progress", "schema_version": 1,
            "state": "compile_started", "repetition": 0,
            "requested_repetitions": 1, "elapsed_ns": 0, "events": 0,
            "capture_bytes": 0, "valid_frames": 0,
            "capture_started": False, "capture_complete": False,
        }
        temporary = progress.with_name(progress.name + ".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, progress)
        time.sleep(0.28)
        state.update({"state": "capture_started", "repetition": 1, "events": 3,
                      "capture_bytes": 64, "valid_frames": 1, "capture_started": True})
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, progress)
        time.sleep(0.28)
        state.update({"state": "complete", "capture_complete": True})
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, progress)
    if os.environ.get("FAKE_PROFILE_STALE"):
        digest = "0" * 64
    capture = {
        "schema_version": 2,
        "envelope": {"major": 2, "minor": 0, "kind": "profile", "compatibility": "backward-compatible-v1"},
        "source": str(source),
        "compiler": {
            "manifest": "compiler-manifest.json",
            "stage1_binary": "elisac-stage1",
            "stage1_sha256": "a" * 64,
            "runtime_object_sha256": "b" * 64,
            "commit": "fake-compiler",
        },
        "summary": {"events": 3, "locations": 2, "dropped": 0, "function_events": 3},
        "quality": {"capture": "complete", "detail": "complete", "reasons": []},
        "source_mapping": {},
        "workload": {"source_sha256": digest},
        "run": {"collection_mode": collection_mode, "outcome": "success", "execution_ms_mean": 1.25,
                "requested_repetitions": requested_repetitions, "completed_repetitions": requested_repetitions,
                "capabilities": {
                    "event_classes": ["function"], "trace": "disabled", "recent_path": "last_256",
                    "timing": "wall", "sampling": "disabled",
                    "sampling_detail": {"status": "disabled", "reason": "mode_not_selected", "scope": "none"},
                    "allocation": {"status": "disabled", "reason": "mode_not_selected", "scope": "none"},
                    "tasks": {"status": "unsupported", "reason": "task_lifecycle_hooks_unavailable", "scope": "none"},
                    "identity": {"status": "compiler_stable_ids", "reason": "compiler_issued_function_and_location_ids", "scope": "capture"},
                }},
        "functions": [{"function": "main", "inclusive_ns": 1000, "self_ns": 700, "completed_calls": 1}],
        "call_edges": [], "stacks": [],
        "locations": [{"function": "main", "line": 1, "count": 1}],
    }
    artifact = {
        "artifact_version": 1,
        "kind": "elisa-profile",
        "manifest": {"capture_format": "profile-json-v2", "compression": "none", "capture_bytes": 1},
        "capture": capture,
    }
    output.write_text(json.dumps(artifact, separators=(",", ":")), encoding="utf-8")
    if os.environ.get("FAKE_PROFILE_MUTATE_SOURCE"):
        source.write_bytes(raw_source + b"\\n# changed while profiling\\n")
    """
)


def fixture(directory: Path, *, extra_env: dict[str, str] | None = None):
    workspace = directory / "Project résumé"
    workspace.mkdir()
    source = workspace / "Counter with spaces.elisa"
    source.write_text("def main() -> i64:\n    return 0\n", encoding="utf-8")
    (workspace / "Project résumé.elisaproject.json").write_text("{}\n", encoding="utf-8")

    tool = directory / "fake profiler.py"
    tool.write_text(FAKE_PROFILER, encoding="utf-8")
    tool.chmod(0o755)

    viewer = directory / "fake profile viewer.py"
    viewer.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_PROFILE_VIEWER_LOG']).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    viewer.chmod(0o755)
    viewer_log = directory / "viewer arguments.json"

    compiler = directory / "fake compiler"
    (compiler / "bin").mkdir(parents=True)
    (compiler / "build" / "runtime").mkdir(parents=True)
    stage1 = compiler / "bin" / "elisac-stage1"
    stage1.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stage1.chmod(0o755)
    (compiler / "build" / "runtime" / "elisacore_runtime.o").write_bytes(b"runtime fixture")

    profiles = directory / "private profile storage"
    env = os.environ.copy()
    env.update(
        {
            "ELISA_PROFILER": str(tool),
            "ELISA_COMPILER_ROOT": str(compiler),
            "ELISA_IDE_PROFILE_ROOT": str(profiles),
            "ELISA_IDE_VIEWER": str(viewer),
            "FAKE_PROFILE_VIEWER_LOG": str(viewer_log),
            "PYTHONUNBUFFERED": "1",
        }
    )
    if extra_env:
        env.update(extra_env)
    return source, tool, profiles, env


def run_runner(source: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), str(source)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def run_open_report(source: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), "--open-latest-report", str(source)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def run_recover_latest(source: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), "--recover-latest", str(source)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def recovered_capture_bytes(source: Path, source_digest: str, manifest: Path, capture_path: Path) -> bytes:
    capture = {
        "schema_version": 2,
        "envelope": {"major": 2, "minor": 0, "kind": "profile", "compatibility": "backward-compatible-v1"},
        "source": str(source.resolve()),
        "compiler": {
            "root": "unavailable-recovery", "branch": "unavailable-recovery",
            "manifest": "unavailable-recovery", "stage1_binary": "unavailable-recovery",
            "stage1_sha256": None, "runtime_object": "unavailable-recovery",
            "runtime_object_sha256": None,
            "commit": "unavailable-recovery",
        },
        "summary": {"events": 2, "locations": 1, "dropped": 0, "function_events": 2},
        "quality": {"capture": "recovered", "reasons": ["recovered_partial"]},
        "source_mapping": {},
        "workload": {"source_sha256": source_digest},
        "run": {
            "collection_mode": "functions", "outcome": "incomplete_artifact",
            "requested_repetitions": 1, "completed_repetitions": 0,
        },
        "functions": [{"function": "main", "inclusive_ns": 12, "self_ns": 12, "completed_calls": 1}],
        "call_edges": [], "stacks": [], "locations": [],
        "recovery": {
            "kind": "partial-capture", "state": "running",
            "manifest": str(manifest), "capture_path": str(capture_path),
            "source_sha256": source_digest, "capture_bytes": 100,
            "valid_frames": 2, "valid_bytes": 80, "tail_truncated": True,
        },
    }
    return json.dumps(capture, separators=(",", ":")).encode("utf-8")


def process_running(pid: int) -> bool:
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(state) and not state.startswith("Z")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="elisa-profile-test-") as temporary:
        directory = Path(temporary)
        source, _tool, profiles, env = fixture(directory)

        completed = run_runner(source, env)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert "Profile capture accepted" in completed.stdout
        assert "Profile configuration: default" in completed.stdout
        assert "slow_path" not in completed.stdout
        assert "main: 1000 ns inclusive" in completed.stdout
        assert "mode: functions" in completed.stdout
        assert "Telemetry coverage:" in completed.stdout
        assert "Allocation lifecycle: disabled; scope none; reason mode_not_selected" in completed.stdout
        assert "Process attach: unavailable" in completed.stdout
        assert "5.0 ms" not in completed.stdout
        captures = list(profiles.rglob("*.elisaprof"))
        assert len(captures) == 1
        assert captures[0].stat().st_mode & 0o777 == 0o600
        assert "Project résumé" in completed.stdout
        assert "Counter with spaces.elisa" in completed.stdout
        task_records = list(profiles.rglob("*.task.json"))
        assert len(task_records) == 1
        task_record = json.loads(task_records[0].read_text(encoding="utf-8"))
        assert task_records[0].stat().st_mode & 0o777 == 0o600
        assert task_record["schema_version"] == 1 and task_record["state"] == "succeeded"
        assert task_record["source"]["path"] == str(source.resolve())
        assert task_record["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert task_record["launch"]["configuration_id"] == "default"
        assert task_record["profiler"]["sha256"] == hashlib.sha256(_tool.read_bytes()).hexdigest()
        assert task_record["compiler"]["commit"] == "fake-compiler"
        assert "Allocation lifecycle: disabled" in task_record["result"]["capability_lines"][2]
        assert task_record["artifact"]["sha256"] == hashlib.sha256(captures[0].read_bytes()).hexdigest()

        configured_dir = directory / "Configured launch"
        configured_dir.mkdir()
        configured_source, _tool, configured_profiles, configured_env = fixture(configured_dir)
        configured_env.pop("ELISA_IDE_PROFILE_ROOT")
        workdir = configured_source.parent / "scenario cwd"
        workdir.mkdir()
        stdin_path = configured_source.parent / "scenario input.txt"
        stdin_path.write_text("fixture input\n", encoding="utf-8")
        args_log = configured_dir / "profiler argv.json"
        configured_env["FAKE_PROFILE_ARGS_LOG"] = str(args_log)
        config_path = configured_source.parent / ".elisa-ide" / "profile-launches.json"
        config_path.parent.mkdir()
        config_path.write_text(json.dumps({
            "schema_version": 1,
            "active_configuration": "scenario",
            "configurations": [{
                "id": "scenario",
                "target": configured_source.name,
                "mode": "sample",
                "repetitions": 3,
                "warmup": 1,
                "timeout_seconds": 12,
                "sample_period_us": 2500,
                "arguments": ["--label", "two words", ""],
                "working_directory": workdir.name,
                "stdin": stdin_path.name,
                "environment": {"PROFILE_LABEL": "scenario value"},
                "random_seed": 17,
                "output_directory": "configured profiles",
            }],
        }), encoding="utf-8")
        configured_capture = run_runner(configured_source, configured_env)
        assert configured_capture.returncode == 0, configured_capture.stderr + configured_capture.stdout
        assert "Profile configuration: scenario; mode=sample; repetitions=3; warmup=1; target timeout=12s" in configured_capture.stdout
        configured_argv = json.loads(args_log.read_text(encoding="utf-8"))
        for option, expected in (
            ("--mode", "sample"), ("--repeat", "3"), ("--warmup", "1"),
            ("--timeout", "12"), ("--sample-period-us", "2500"),
            ("--cwd", str(workdir.resolve())), ("--stdin", str(stdin_path.resolve())),
            ("--env", "PROFILE_LABEL=scenario value"), ("--random-seed", "17"),
        ):
            assert configured_argv[configured_argv.index(option) + 1] == expected
        separator = configured_argv.index("--")
        assert configured_argv[separator + 1:] == ["--label", "two words", ""]
        assert len(list((configured_source.parent / "configured profiles").rglob("*.elisaprof"))) == 1
        assert not list(configured_profiles.rglob("*.elisaprof"))
        configured_task = next((configured_source.parent / "configured profiles").rglob("*.task.json"))
        configured_task_record = json.loads(configured_task.read_text(encoding="utf-8"))
        assert configured_task_record["state"] == "succeeded"
        assert configured_task_record["launch"]["configuration_id"] == "scenario"
        assert configured_task_record["launch"]["sample_period_us"] == 2500
        assert configured_task_record["launch"]["environment_override_keys"] == ["PROFILE_LABEL"]
        assert "scenario value" not in configured_task.read_text(encoding="utf-8")
        configured_report = run_open_report(configured_source, configured_env)
        assert configured_report.returncode == 0, configured_report.stderr + configured_report.stdout
        assert "Opened html profile report" in configured_report.stdout

        # A mismatched or malformed active launch cannot start a profiler job.
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        config_data["configurations"][0]["environment"] = {"ELISA_PROFILE_FD": "9"}
        config_path.write_text(json.dumps(config_data), encoding="utf-8")
        invalid_config = run_runner(configured_source, configured_env)
        assert invalid_config.returncode == 1
        assert "may not set profiler transport variable" in invalid_config.stderr
        assert len(json.loads(args_log.read_text(encoding="utf-8"))) == len(configured_argv)

        opened = run_open_report(source, env)
        assert opened.returncode == 0, opened.stderr + opened.stdout
        assert "Opened html profile report" in opened.stdout
        reports = list(profiles.rglob("*.html"))
        assert len(reports) == 1
        assert reports[0].read_text(encoding="utf-8").startswith("<!doctype html>")
        assert reports[0].stat().st_mode & 0o777 == 0o600
        viewer_log = Path(env["FAKE_PROFILE_VIEWER_LOG"])
        assert json.loads(viewer_log.read_text(encoding="utf-8")) == [str(reports[0])]

        # The report action follows the saved source digest and refuses to
        # launch a viewer when the saved file no longer matches the capture.
        source.write_text(source.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        changed_report = run_open_report(source, env)
        assert changed_report.returncode == 1
        assert "no validated profile capture matches" in changed_report.stderr
        assert json.loads(viewer_log.read_text(encoding="utf-8")) == [str(reports[0])]

        progress_dir = directory / "progress"
        progress_dir.mkdir()
        progress_source, _tool, progress_profiles, progress_env = fixture(
            progress_dir, extra_env={"FAKE_PROFILE_PROGRESS": "1"}
        )
        progress_capture = run_runner(progress_source, progress_env)
        assert progress_capture.returncode == 0, progress_capture.stderr + progress_capture.stdout
        assert "Profile progress: compiling (repetition 0/1; 0 events; 0 valid frames)" in progress_capture.stdout
        assert "Profile progress: capturing (repetition 1/1; 3 events; 1 valid frames)" in progress_capture.stdout
        assert "Profile progress: capture complete (repetition 1/1; 3 events; 1 valid frames)" in progress_capture.stdout
        assert json.loads(next(progress_profiles.rglob("*.task.json")).read_text(encoding="utf-8"))["state"] == "succeeded"

        stale_env = env | {"FAKE_PROFILE_STALE": "1"}
        stale = run_runner(source, stale_env)
        assert stale.returncode == 1
        assert "source_sha256" in stale.stderr
        assert "artifact retained" in stale.stderr
        assert "Profile capture accepted" not in stale.stdout
        failed_tasks = [json.loads(path.read_text(encoding="utf-8")) for path in profiles.rglob("*.task.json")]
        assert sum(task["state"] == "failed" for task in failed_tasks) == 1

        mutation_dir = directory / "mutation"
        mutation_dir.mkdir()
        mutated_source, _tool, mutation_profiles, mutation_env = fixture(
            mutation_dir, extra_env={"FAKE_PROFILE_MUTATE_SOURCE": "1"}
        )
        changed = run_runner(mutated_source, mutation_env)
        assert changed.returncode == 1
        assert "source changed during profiling" in changed.stderr
        assert len(list(mutation_profiles.rglob("*.elisaprof"))) == 1

    with tempfile.TemporaryDirectory(prefix="elisa-profile-recovery-") as temporary:
        directory = Path(temporary)
        source, tool, profiles, env = fixture(directory)
        project_root = source.parent.resolve()
        project_key = hashlib.sha256(os.fsencode(project_root)).hexdigest()[:20]
        profile_directory = profiles / project_key
        profile_directory.mkdir(parents=True, mode=0o700)
        profile_directory.chmod(0o700)
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

        capture_root = Path(tempfile.mkdtemp(prefix="elisa-profiler-native-test-"))
        capture_path = capture_root / "capture.txt"
        capture_path.write_bytes(b"ELISA_PROFILE partial framed data\n")
        capture_path.chmod(0o600)
        manifest_path = profile_directory / "interrupted.elisaprof.manifest.json"
        manifest = {
            "kind": "elisa_profile_capture_manifest", "schema_version": 1,
            "state": "running", "source": str(source.resolve()),
            "source_sha256": source_digest, "collection_mode": "functions",
            "capture_path": str(capture_path), "requested_repetitions": 1,
            "completed_repetitions": 0, "successful_repetitions": 0,
            "failed_repetitions": 0, "events": 0, "capture_started": True,
            "capture_complete": False,
            "capture_index": {"format": "record-framed-v1", "bytes": 32, "valid_frames": 2, "valid_bytes": 24},
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        recovered_raw = directory / "recovered capture.json"
        recovered_raw.write_bytes(recovered_capture_bytes(source, source_digest, manifest_path, capture_path))
        args_log = directory / "recovery argv.json"
        output_mode = directory / "recovery output mode.txt"
        recovery_env = env | {
            "FAKE_RECOVERY_CAPTURE": str(recovered_raw),
            "FAKE_PROFILE_ARGS_LOG": str(args_log),
            "FAKE_RECOVERY_OUTPUT_MODE": str(output_mode),
        }

        recovered = run_recover_latest(source, recovery_env)
        assert recovered.returncode == 0, recovered.stderr + recovered.stdout
        assert "Profile recovery accepted" in recovered.stdout
        assert "Capture: recovered" in recovered.stdout
        assert "Compiler revision: not recorded" in recovered.stdout
        argv = json.loads(args_log.read_text(encoding="utf-8"))
        assert argv[:2] == ["recover", str(manifest_path)]
        assert argv[argv.index("--format") + 1] == "json"
        assert output_mode.read_text(encoding="utf-8") == "0o600"

        artifacts = list(profile_directory.glob("*.recovered.elisaprof"))
        assert len(artifacts) == 1
        assert artifacts[0].stat().st_mode & 0o777 == 0o600
        recovered_artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
        assert recovered_artifact["capture"]["quality"]["capture"] == "recovered"
        assert recovered_artifact["capture"]["compiler"]["stage1_sha256"] is None
        task_path = artifacts[0].with_name(artifacts[0].name + ".task.json")
        task = json.loads(task_path.read_text(encoding="utf-8"))
        assert task_path.stat().st_mode & 0o777 == 0o600
        assert task["attempt"] == "recovery" and task["state"] == "recovered"
        assert task["recovery"]["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()

        source.write_text(source.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        stale = run_recover_latest(source, recovery_env)
        assert stale.returncode == 1
        assert "no private recoverable partial capture" in stale.stderr
        assert len(list(profile_directory.glob("*.recovered.elisaprof"))) == 1

    with tempfile.TemporaryDirectory(prefix="elisa-profile-recovery-invalid-") as temporary:
        directory = Path(temporary)
        source, _tool, profiles, env = fixture(directory)
        project_root = source.parent.resolve()
        project_key = hashlib.sha256(os.fsencode(project_root)).hexdigest()[:20]
        profile_directory = profiles / project_key
        profile_directory.mkdir(parents=True, mode=0o700)
        (profile_directory / "broken.manifest.json").write_text('{"kind":', encoding="utf-8")
        args_log = directory / "recovery argv.json"
        invalid = run_recover_latest(source, env | {"FAKE_PROFILE_ARGS_LOG": str(args_log)})
        assert invalid.returncode == 1
        assert "no private recoverable partial capture" in invalid.stderr
        assert not args_log.exists(), "malformed manifest reached the profiler process"

    with tempfile.TemporaryDirectory(prefix="elisa-profile-cancel-") as temporary:
        directory = Path(temporary)
        child_pid_file = directory / "profile-child.pid"
        source, _tool, profiles, env = fixture(
            directory,
            extra_env={
                "FAKE_PROFILE_SLEEP": "1",
                "FAKE_PROFILE_CHILD_PID_FILE": str(child_pid_file),
            },
        )
        runner = subprocess.Popen(
            [sys.executable, str(RUNNER), str(source)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and runner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_file.exists(), "fake profiler did not start its descendant"
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        runner.send_signal(signal.SIGTERM)
        stdout, stderr = runner.communicate(timeout=5)
        assert runner.returncode == 128 + signal.SIGTERM, stdout + stderr
        cancelled_tasks = list(profiles.rglob("*.task.json"))
        assert len(cancelled_tasks) == 1
        cancelled_record = json.loads(cancelled_tasks[0].read_text(encoding="utf-8"))
        assert cancelled_record["state"] == "cancelled"
        assert "cancelled" in cancelled_record["error"]
        deadline = time.monotonic() + 2
        while process_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not process_running(child_pid), f"profile descendant {child_pid} survived cancellation"

    print("ok profile runner")


if __name__ == "__main__":
    main()
