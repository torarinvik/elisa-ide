from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.profile.profile_config import ProfileConfigError, load_profile_configuration  # noqa: E402


def write_config(project: Path, config: str | dict) -> Path:
    path = project / ".elisa-ide" / "profile-launches.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = config if isinstance(config, str) else json.dumps(config)
    path.write_text(raw, encoding="utf-8")
    return path


def config_for(target: Path, **overrides: object) -> dict:
    selected = {
        "id": "default",
        "target": target.name,
        "mode": "functions",
    }
    selected.update(overrides)
    return {
        "schema_version": 1,
        "active_configuration": "default",
        "configurations": [selected],
    }


def expect_error(project: Path, source: Path, config: str | dict, phrase: str) -> None:
    write_config(project, config)
    try:
        load_profile_configuration(source.resolve(), project.resolve())
    except ProfileConfigError as exc:
        assert phrase in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ProfileConfigError containing {phrase!r}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="elisa-profile-config-") as temporary:
        project = Path(temporary) / "Project"
        project.mkdir()
        source = project / "main.elisa"
        source.write_text("def main() -> i64:\n    return 0\n", encoding="utf-8")
        (project / "input.txt").write_text("input\n", encoding="utf-8")
        (project / "run cwd").mkdir()

        default = load_profile_configuration(source.resolve(), project.resolve())
        assert default.configuration_id == "default"
        assert default.target == source.resolve()
        assert default.mode == "functions"
        assert default.repetitions == 1
        assert default.timeout_seconds == 30
        assert default.working_directory == project.resolve()

        write_config(project, config_for(
            source,
            mode="sample",
            repetitions=3,
            warmup=2,
            timeout_seconds=10,
            sample_period_us=2500,
            arguments=["--case", "with spaces"],
            working_directory="run cwd",
            stdin="input.txt",
            environment={"LABEL": "friendly name"},
            random_seed=7,
            output_directory="profiles",
        ))
        selected = load_profile_configuration(source.resolve(), project.resolve())
        assert selected.configuration_id == "default"
        assert selected.mode == "sample"
        assert selected.repetitions == 3 and selected.warmup == 2
        assert selected.timeout_seconds == 10 and selected.sample_period_us == 2500
        assert selected.arguments == ("--case", "with spaces")
        assert selected.working_directory == (project / "run cwd").resolve()
        assert selected.stdin == (project / "input.txt").resolve()
        assert selected.environment == (("LABEL", "friendly name"),)
        assert selected.random_seed == 7
        assert selected.output_directory == (project / "profiles").resolve()

        expect_error(project, source, '{"schema_version":1,"schema_version":1}', "duplicate configuration key")
        expect_error(project, source, config_for(source, typo_timeout=4), "unknown field")
        expect_error(project, source, config_for(source, mode="sampling"), "must be one of")
        expect_error(project, source, config_for(source, sample_period_us=10), "must be an integer from 100 through 1000000")
        expect_error(project, source, config_for(source, sample_period_us=1000), "requires mode 'sample'")
        expect_error(project, source, config_for(source, timeout_seconds=1800, repetitions=2), "IDE job limit")
        expect_error(project, source, config_for(source, arguments=["bad\x00argument"]), "without NUL")
        expect_error(project, source, config_for(source, environment={"ELISA_PROFILE_FD": "5"}), "transport variable")
        expect_error(project, source, config_for(source, environment={"ELISA_RANDOM_SEED": "5"}, random_seed=5), "cannot set both")
        expect_error(project, source, config_for(project / "other.elisa"), "but the open source is")
        expect_error(project, source, config_for(source, stdin="missing.txt"), "not an existing file")
        expect_error(project, source, config_for(source, working_directory="missing"), "not an existing directory")

        # Unknown active ids, duplicate ids, and unsupported schema versions are rejected.
        bad_active = config_for(source)
        bad_active["active_configuration"] = "missing"
        expect_error(project, source, bad_active, "does not match a configured id")
        duplicate_ids = config_for(source)
        duplicate_ids["configurations"].append(dict(duplicate_ids["configurations"][0]))
        expect_error(project, source, duplicate_ids, "ids must be unique")
        expect_error(project, source, {**config_for(source), "schema_version": 2}, "schema_version must be 1")

        config_path = write_config(project, config_for(source))
        config_path.write_text("", encoding="utf-8")
        try:
            load_profile_configuration(source.resolve(), project.resolve())
        except ProfileConfigError as exc:
            assert "size must be between" in str(exc)
        else:
            raise AssertionError("empty configuration should be rejected")

        config_path.unlink()
        config_path.symlink_to(project / "outside-config.json")
        try:
            load_profile_configuration(source.resolve(), project.resolve())
        except ProfileConfigError as exc:
            assert "regular non-symlink" in str(exc)
        else:
            raise AssertionError("symlink configuration should be rejected")

    print("ok profile config")


if __name__ == "__main__":
    main()
