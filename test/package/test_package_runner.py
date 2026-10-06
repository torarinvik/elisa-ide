from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import signal
import stat
import time
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(ROOT))

from worker.package import package_runner


def _package(root: Path, *, dependencies: dict[str, object] | None = None) -> Path:
    (root / "tests").mkdir(parents=True)
    source = root / "src" / "main.elisa"
    source.parent.mkdir()
    source.write_text("def main() -> i64:\n    return 0\n", encoding="utf-8")
    (root / "tests" / "smoke.elisa").write_text("def test() -> i64:\n    return 1\n", encoding="utf-8")
    manifest = {
        "schema-version": 1,
        "package": {"name": "package-fixture", "version": "0.1.0"},
        "targets": [
            {"name": "app", "kind": "bin", "entry": "src/main.elisa"},
            {"name": "smoke", "kind": "test", "entry": "tests/smoke.elisa"},
            {"name": "deep-check", "kind": "test", "entry": "tests/smoke.elisa"},
        ],
        "dependencies": dependencies or {},
        "dev-dependencies": {},
    }
    (root / "elisapkg.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


def _fake_tool(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "started = os.environ.get('FAKE_TOOL_STARTED_FILE')\n"
        "if started: Path(started).write_text('started', encoding='utf-8')\n"
        "output_bytes = int(os.environ.get('FAKE_TOOL_OUTPUT_BYTES', '0') or '0')\n"
        "if output_bytes:\n"
        "    os.write(1, b'\\x1b[31m' + b'x' * output_bytes)\n"
        "    os.write(2, b'warning: ' + b'y' * output_bytes)\n"
        "descendant_file = os.environ.get('FAKE_TOOL_DESCENDANT_FILE')\n"
        "if descendant_file:\n"
        "    import subprocess\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "    Path(descendant_file).write_text(str(child.pid), encoding='ascii')\n"
        "time.sleep(float(os.environ.get('FAKE_TOOL_SLEEP', '0') or '0'))\n"
        "exit_code = int(os.environ.get('FAKE_TOOL_EXIT', '0') or '0')\n"
        "if exit_code:\n"
        "    print('fake elisapkg test failed')\n"
        "    print('[ RUN      ] runner_failure')\n"
        "    print(f'[ FAILED   ] runner_failure (exit {exit_code})')\n"
        "    print('[ STDOUT   ] runner_failure')\n"
        "    print('    captured assertion detail')\n"
        "    print('[ SUMMARY  ] 1 test(s) selected; passed=0 skipped=0 failed=1')\n"
        "    raise SystemExit(exit_code)\n"
        "print('fake elisapkg tests passed')\n"
        "print('[ RUN      ] runner_smoke')\n"
        "print('[       OK ] runner_smoke')\n"
        "print('[ SUMMARY  ] 1 test(s) selected; passed=1 skipped=0 failed=0')\n"
        "print('argv=' + json.dumps(sys.argv[1:]))\n"
        "print('cwd=' + os.getcwd())\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class PackageRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="elisa-package-runner-")
        self.root = Path(self.temporary.name)
        self.tool = _fake_tool(self.root / "tools" / "elisapkg")
        self.env = {"ELISAPKG": os.fspath(self.tool), "PATH": os.environ.get("PATH", "")}
        self.options = {"env": self.env, "ide_root": self.root, "trusted_nearby": ()}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_plan_discovers_tests_and_binds_tool_and_manifest(self) -> None:
        source = _package(self.root / "package")
        plan = package_runner.prepare_plan(os.fspath(source), **self.options)
        self.assertEqual(plan["package_name"], "package-fixture")
        self.assertEqual(plan["tests"], ("smoke", "deep-check"))
        self.assertEqual(plan["root"], source.resolve().parent.parent)
        self.assertTrue(plan["manifest_digest"])
        self.assertEqual(len(plan["approval"]), 64)
        self.assertEqual(len(plan["input_tree_digest"]), 64)
        display = package_runner.format_plan(plan)
        self.assertIn("Discovered test targets (2): smoke, deep-check", display)
        self.assertIn(f"Command: {self.tool.resolve()} test --offline --test smoke", display)
        self.assertIn("No project code has run.", display)
        self.assertIn(f"Package input SHA-256: {plan['input_tree_digest']}", display)
        self.assertIn("TARGETS1 smoke deep-check", display)
        self.assertIn(f"PLAN1 {plan['approval']} smoke", display)

    def test_approval_tracks_package_sources_and_local_dependency_sources(self) -> None:
        dependency = _package(self.root / "local-dependency")
        source = _package(
            self.root / "project",
            dependencies={"local": {"path": "../local-dependency"}},
        )
        original = package_runner.prepare_plan(os.fspath(source), **self.options)
        dependency.write_text(
            "def main() -> i64:\n    return 9\n", encoding="utf-8"
        )
        changed_dependency = package_runner.prepare_plan(os.fspath(source), **self.options)
        self.assertNotEqual(original["approval"], changed_dependency["approval"])
        self.assertNotEqual(original["input_tree_digest"], changed_dependency["input_tree_digest"])

    def test_approval_tracks_tests_but_ignores_generated_build_outputs(self) -> None:
        source = _package(self.root / "project")
        original = package_runner.prepare_plan(os.fspath(source), **self.options)
        (source.parent.parent / "build").mkdir()
        (source.parent.parent / "build" / "generated.cache").write_text("first", encoding="utf-8")
        with_build_output = package_runner.prepare_plan(os.fspath(source), **self.options)
        self.assertEqual(original["approval"], with_build_output["approval"])
        (source.parent.parent / "tests" / "smoke.elisa").write_text(
            "def test() -> i64:\n    return 2\n", encoding="utf-8"
        )
        changed_source = package_runner.prepare_plan(os.fspath(source), **self.options)
        self.assertNotEqual(original["approval"], changed_source["approval"])

    def test_success_is_marked_stale_when_package_input_changes_during_test(self) -> None:
        source = _package(self.root / "project")
        plan = package_runner.prepare_plan(os.fspath(source), **self.options)
        started = self.root / "tool-started"
        script = (
            "import os, sys; from worker.package.package_runner import run_test; "
            "raise SystemExit(run_test(sys.argv[1], 'smoke', sys.argv[2], "
            "env=dict(os.environ, ELISAPKG=sys.argv[3], FAKE_TOOL_STARTED_FILE=sys.argv[4], "
            "FAKE_TOOL_SLEEP='0.5'), ide_root=sys.argv[5], trusted_nearby=()))"
        )
        command = [
            sys.executable,
            "-c",
            script,
            os.fspath(source),
            str(plan["approval"]),
            os.fspath(self.tool),
            os.fspath(started),
            os.fspath(self.root),
        ]
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(started.exists(), "fake package manager did not start")
        (source.parent.parent / "tests" / "smoke.elisa").write_text(
            "def test() -> i64:\n    return 3\n", encoding="utf-8"
        )
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, package_runner.STALE_SUCCESS_EXIT, stderr)
        self.assertIn("Package manager exit code: 0", stdout)
        self.assertIn("Package test result is stale", stdout)
        records = list((source.parent.parent / ".elisa-ide" / "package-test-history").glob("*.task.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text(encoding="utf-8"))["state"], "stale")

    def test_stop_signal_terminates_and_reaps_package_manager_descendants(self) -> None:
        source = _package(self.root / "project")
        plan = package_runner.prepare_plan(os.fspath(source), **self.options)
        started = self.root / "tool-started"
        descendant = self.root / "tool-descendant.pid"
        script = (
            "import os, sys; from worker.package.package_runner import run_test; "
            "raise SystemExit(run_test(sys.argv[1], 'smoke', sys.argv[2], "
            "env=dict(os.environ, ELISAPKG=sys.argv[3], FAKE_TOOL_STARTED_FILE=sys.argv[4], "
            "FAKE_TOOL_DESCENDANT_FILE=sys.argv[5], FAKE_TOOL_SLEEP='30'), "
            "ide_root=sys.argv[6], trusted_nearby=()))"
        )
        command = [
            sys.executable,
            "-c",
            script,
            os.fspath(source),
            str(plan["approval"]),
            os.fspath(self.tool),
            os.fspath(started),
            os.fspath(descendant),
            os.fspath(self.root),
        ]
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while not descendant.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(descendant.exists(), "fake package manager descendant did not start")
        descendant_pid = int(descendant.read_text(encoding="ascii"))
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
        self.assertNotIn("Package manager exit code:", stdout)
        history_dir = source.parent.parent / ".elisa-ide" / "package-test-history"
        records = list(history_dir.glob("*.task.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text(encoding="utf-8"))["state"], "cancelled")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail("package manager descendant remained alive after Stop signal")

    def test_registry_dependency_is_refused_before_compiler_invocation(self) -> None:
        source = _package(
            self.root / "package",
            dependencies={"remote": {"registry": "primary", "version": "^1.0.0"}},
        )
        with self.assertRaisesRegex(package_runner.PackageTaskError, "local path dependencies only"):
            package_runner.prepare_plan(os.fspath(source), **self.options)

    def test_explicit_package_manager_overrides_trusted_nearby(self) -> None:
        source = _package(self.root / "package")
        nearby = _fake_tool(self.root / "nearby" / "elisapkg")
        explicit = _fake_tool(self.root / "explicit" / "elisapkg")
        environment = dict(self.env, ELISA_IDE_ELISAPKG=os.fspath(explicit))
        plan = package_runner.prepare_plan(
            os.fspath(source), env=environment, ide_root=self.root, trusted_nearby=(nearby,)
        )
        self.assertEqual(plan["tool"].path, explicit.resolve())
        self.assertEqual(plan["tool"].selected_by, "explicit")

    def test_run_requires_current_approval_and_executes_structured_argv(self) -> None:
        source = _package(self.root / "project with spaces")
        plan = package_runner.prepare_plan(os.fspath(source), **self.options)
        script = (
            "import sys; from worker.package.package_runner import run_test; "
            "raise SystemExit(run_test(sys.argv[1], sys.argv[2], sys.argv[3], "
            "env={'ELISAPKG': sys.argv[4], 'PATH': sys.argv[5], 'FAKE_SECRET': 'value-hidden'}, "
            "ide_root=sys.argv[6], trusted_nearby=()))"
        )
        command = [
            sys.executable, "-c", script, os.fspath(source), "smoke", plan["approval"],
            os.fspath(self.tool), self.env["PATH"], os.fspath(self.root),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("fake elisapkg tests passed", completed.stdout)
        self.assertIn('argv=["test", "--offline", "--test", "smoke"]', completed.stdout)
        self.assertIn(f"cwd={source.resolve().parent.parent}", completed.stdout)
        history_dir = source.parent.parent / ".elisa-ide" / "package-test-history"
        history_path, = history_dir.glob("*.task.json")
        history_raw = history_path.read_text(encoding="utf-8")
        history = json.loads(history_raw)
        self.assertEqual(history["state"], "succeeded")
        self.assertEqual(history["target"], "smoke")
        self.assertEqual(history["result"]["package_manager_exit_code"], 0)
        self.assertEqual(history["result"]["test_cases"], [{"name": "runner_smoke", "state": "passed", "detail": "", "output": "", "output_truncated": False}])
        self.assertEqual(history["result"]["test_summary"], {"selected": 1, "passed": 1, "skipped": 0, "failed": 0})
        self.assertIn("FAKE_SECRET", history["launch"]["environment_variable_names"])
        self.assertNotIn("value-hidden", history_raw)
        self.assertIn("fake elisapkg tests passed", base64.b64decode(history["result"]["stdout_base64"]).decode())
        self.assertEqual(stat.S_IMODE(history_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(history_dir.stat().st_mode), 0o700)
        history_output = package_runner.format_package_history(os.fspath(source))
        self.assertIn("succeeded | smoke | exit 0", history_output)
        history_detail = package_runner.format_package_history(os.fspath(source), 0)
        self.assertIn("State: succeeded | target: smoke | package-manager exit: 0", history_detail)
        self.assertIn("fake elisapkg tests passed", history_detail)
        self.assertIn("Ctrl/Cmd+Option+Shift+J/K", history_detail)
        explorer = package_runner.format_package_test_explorer(os.fspath(source), 0, 0)
        self.assertIn("PASSED runner_smoke", explorer)
        self.assertIn("Reported cases: 1 | summary: 1 passed, 0 skipped, 0 failed", explorer)
        explorer_command = subprocess.run(
            [sys.executable, os.fspath(ROOT / "worker" / "package" / "package_runner.py"), "test-explorer", os.fspath(source), "0", "0"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(explorer_command.returncode, 0, explorer_command.stderr)
        self.assertIn("Selected case 1 of 1", explorer_command.stdout)
        history_command = subprocess.run(
            [sys.executable, os.fspath(ROOT / "worker" / "package" / "package_runner.py"), "history", os.fspath(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(history_command.returncode, 0, history_command.stderr)
        self.assertIn("succeeded | smoke | exit 0", history_command.stdout)

        manifest = source.parent.parent / "elisapkg.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["package"]["version"] = "0.2.0"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        stale = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("changed after the trust preview", stale.stderr)
        self.assertNotIn("fake elisapkg tests passed", stale.stdout)

    def test_history_output_is_bounded_and_terminal_controls_are_visible(self) -> None:
        source = _package(self.root / "package")
        plan = package_runner.prepare_plan(os.fspath(source), **self.options)
        script = (
            "import os, sys; from worker.package.package_runner import run_test; "
            "raise SystemExit(run_test(sys.argv[1], 'smoke', sys.argv[2], "
            "env=dict(os.environ, ELISAPKG=sys.argv[3], FAKE_TOOL_OUTPUT_BYTES='9000'), "
            "ide_root=sys.argv[4], trusted_nearby=()))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, os.fspath(source), str(plan["approval"]), os.fspath(self.tool), os.fspath(self.root)],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        history_dir = source.parent.parent / ".elisa-ide" / "package-test-history"
        history_path, = history_dir.glob("*.task.json")
        record = json.loads(history_path.read_text(encoding="utf-8"))
        result = record["result"]
        self.assertTrue(result["output_capture_truncated"])
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])
        self.assertLessEqual(len(base64.b64decode(result["stdout_base64"])), package_runner.MAX_PACKAGE_HISTORY_OUTPUT_PER_STREAM)
        self.assertLessEqual(len(base64.b64decode(result["stderr_base64"])), package_runner.MAX_PACKAGE_HISTORY_OUTPUT_PER_STREAM)
        detail = package_runner.format_package_history(os.fspath(source), 0)
        self.assertIn(r"\x1b[31m", detail)
        self.assertIn("history capture truncated", detail)

    def test_failed_exit_is_recorded_and_recent_history_is_bounded(self) -> None:
        source = _package(self.root / "package")
        plan = package_runner.prepare_plan(os.fspath(source), **self.options)
        script = (
            "import os, sys; from worker.package.package_runner import run_test; "
            "raise SystemExit(run_test(sys.argv[1], 'smoke', sys.argv[2], "
            "env=dict(os.environ, ELISAPKG=sys.argv[3], FAKE_TOOL_EXIT='7'), "
            "ide_root=sys.argv[4], trusted_nearby=()))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, os.fspath(source), str(plan["approval"]), os.fspath(self.tool), os.fspath(self.root)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        history_dir = source.parent.parent / ".elisa-ide" / "package-test-history"
        only_record, = history_dir.glob("*.task.json")
        failed_record = json.loads(only_record.read_text(encoding="utf-8"))
        self.assertEqual(failed_record["state"], "failed")
        self.assertEqual(failed_record["result"]["test_cases"][0]["state"], "failed")
        explorer = package_runner.format_package_test_explorer(os.fspath(source), 0, 0)
        self.assertIn("captured assertion detail", explorer)

        for index in range(package_runner.MAX_PACKAGE_HISTORY_RECORDS + 6):
            started_at = f"2026-10-06T12:00:{index:02d}.000Z"
            record = {
                "schema_version": 1,
                "task_id": f"20261006T1200{index:02d}000000Z-{index:012x}",
                "state": "succeeded",
                "started_at": started_at,
                "finished_at": started_at,
                "target": f"target-{index}",
                "result": {"package_manager_exit_code": 0},
            }
            package_runner._write_package_history_record(source.parent.parent, record)
        records = list(history_dir.glob("*.task.json"))
        self.assertEqual(len(records), package_runner.MAX_PACKAGE_HISTORY_RECORDS)
        history_output = package_runner.format_package_history(os.fspath(source))
        self.assertIn("target-69", history_output)
        self.assertNotIn("target-00", history_output)

    def test_duplicate_test_targets_are_rejected(self) -> None:
        source = _package(self.root / "package")
        manifest_path = source.parent.parent / "elisapkg.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["targets"].append({"name": "smoke", "kind": "test", "entry": "tests/smoke.elisa"})
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(package_runner.PackageTaskError, "repeats test target"):
            package_runner.prepare_plan(os.fspath(source), **self.options)

    def test_manifest_symlink_and_non_test_package_are_refused(self) -> None:
        source = _package(self.root / "package")
        manifest = source.parent.parent / "elisapkg.json"
        external = self.root / "external.json"
        external.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(external)
        with self.assertRaisesRegex(package_runner.PackageTaskError, "non-symlink"):
            package_runner.prepare_plan(os.fspath(source), **self.options)

        manifest.unlink()
        data = json.loads(external.read_text(encoding="utf-8"))
        data["targets"] = [target for target in data["targets"] if target["kind"] != "test"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(package_runner.PackageTaskError, "declares no test targets"):
            package_runner.prepare_plan(os.fspath(source), **self.options)


if __name__ == "__main__":
    unittest.main()
