"""Validated project-local launch configurations for managed profile captures."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_CONFIG_BYTES = 1 << 20
MAX_CONFIGURATIONS = 32
MAX_ARGUMENTS = 128
MAX_ARGUMENT_BYTES = 4096
MAX_TOTAL_ARGUMENT_BYTES = 32 << 10
MAX_ENVIRONMENT = 32
MAX_ENVIRONMENT_VALUE_BYTES = 8192
MAX_JOB_SECONDS = 30 * 60
MODES = {"full", "functions", "statements", "values", "sample", "diagnostic"}
_CONFIG_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROFILER_ENVIRONMENT = {
    "ELISA_PROFILE_FD",
    "ELISA_PROFILE_FRAMED",
    "ELISA_PROFILE_THREAD_RECORDS",
    "ELISA_PROFILE_MODE",
    "ELISA_PROFILE_EVENT_TRACE",
    "ELISA_PROFILE_EVENT_TRACE_LIMIT",
    "ELISA_PROFILE_SAMPLE_PERIOD_US",
    "ELISA_PROFILE_MAX_CAPTURE_BYTES",
    "ELISA_PROFILE_RECENT_PATH",
}


class ProfileConfigError(ValueError):
    """A project profile launch configuration is malformed or unsafe to use."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileConfigError(f"duplicate configuration key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProfileConfigError(f"non-finite configuration number {value!r}")


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProfileConfigError(f"{context} contains unknown field(s): {', '.join(unknown)}")


def _text(value: Any, context: str, *, max_bytes: int = 4096, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or "\x00" in value:
        raise ProfileConfigError(f"{context} must be a {'possibly empty ' if allow_empty else ''}string without NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ProfileConfigError(f"{context} exceeds {max_bytes} UTF-8 bytes")
    return value


def _integer(value: Any, context: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProfileConfigError(f"{context} must be an integer from {minimum} through {maximum}")
    return value


def _resolve_path(value: str, root: Path) -> Path:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProfileConfigError(f"could not resolve configured path {value!r}: {exc}") from exc


@dataclass(frozen=True)
class ProfileLaunchConfig:
    """One resolved, project-scoped profiler launch configuration."""

    configuration_id: str
    target: Path
    mode: str
    repetitions: int
    warmup: int
    timeout_seconds: int
    sample_period_us: int | None
    arguments: tuple[str, ...]
    working_directory: Path
    stdin: Path | None
    environment: tuple[tuple[str, str], ...]
    random_seed: int | None
    output_directory: Path | None


def _parse_configuration(value: Any, root: Path) -> ProfileLaunchConfig:
    if not isinstance(value, dict):
        raise ProfileConfigError("each profile configuration must be an object")
    allowed = {
        "id", "target", "mode", "repetitions", "warmup", "timeout_seconds",
        "sample_period_us", "arguments", "working_directory", "stdin",
        "environment", "random_seed", "output_directory",
    }
    _keys(value, allowed, "profile configuration")
    configuration_id = _text(value.get("id"), "configuration id", max_bytes=64)
    if not _CONFIG_ID.fullmatch(configuration_id):
        raise ProfileConfigError("configuration id may contain only letters, digits, dot, underscore, and hyphen")

    target_text = _text(value.get("target"), f"{configuration_id}.target")
    target = _resolve_path(target_text, root)
    mode = value.get("mode", "functions")
    if not isinstance(mode, str) or mode not in MODES:
        raise ProfileConfigError(f"{configuration_id}.mode must be one of {', '.join(sorted(MODES))}")
    repetitions = _integer(value.get("repetitions", 1), f"{configuration_id}.repetitions", 1, 100)
    warmup = _integer(value.get("warmup", 0), f"{configuration_id}.warmup", 0, 10)
    timeout = _integer(value.get("timeout_seconds", 30), f"{configuration_id}.timeout_seconds", 1, 1800)
    if timeout * (repetitions + warmup) > MAX_JOB_SECONDS:
        raise ProfileConfigError(
            f"{configuration_id} can run for more than the {MAX_JOB_SECONDS}-second IDE job limit"
        )

    sample_period: int | None = None
    if "sample_period_us" in value:
        sample_period = _integer(value["sample_period_us"], f"{configuration_id}.sample_period_us", 100, 1_000_000)
        if mode != "sample":
            raise ProfileConfigError(f"{configuration_id}.sample_period_us requires mode 'sample'")

    raw_arguments = value.get("arguments", [])
    if not isinstance(raw_arguments, list) or len(raw_arguments) > MAX_ARGUMENTS:
        raise ProfileConfigError(f"{configuration_id}.arguments must be an array of at most {MAX_ARGUMENTS} strings")
    arguments = tuple(
        _text(item, f"{configuration_id}.arguments[{index}]", max_bytes=MAX_ARGUMENT_BYTES, allow_empty=True)
        for index, item in enumerate(raw_arguments)
    )
    if sum(len(item.encode("utf-8")) for item in arguments) > MAX_TOTAL_ARGUMENT_BYTES:
        raise ProfileConfigError(f"{configuration_id}.arguments exceeds the {MAX_TOTAL_ARGUMENT_BYTES}-byte total limit")

    working_text = _text(value.get("working_directory", "."), f"{configuration_id}.working_directory")
    working_directory = _resolve_path(working_text, root)
    if not working_directory.is_dir():
        raise ProfileConfigError(f"{configuration_id}.working_directory is not an existing directory: {working_directory}")

    stdin: Path | None = None
    if "stdin" in value:
        stdin_text = _text(value["stdin"], f"{configuration_id}.stdin")
        stdin = _resolve_path(stdin_text, root)
        if not stdin.is_file():
            raise ProfileConfigError(f"{configuration_id}.stdin is not an existing file: {stdin}")

    raw_environment = value.get("environment", {})
    if not isinstance(raw_environment, dict) or len(raw_environment) > MAX_ENVIRONMENT:
        raise ProfileConfigError(f"{configuration_id}.environment must be an object of at most {MAX_ENVIRONMENT} entries")
    environment: list[tuple[str, str]] = []
    for name, raw_value in raw_environment.items():
        if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ProfileConfigError(f"{configuration_id}.environment contains an invalid variable name")
        if name in _PROFILER_ENVIRONMENT:
            raise ProfileConfigError(f"{configuration_id}.environment may not set profiler transport variable {name}")
        if name == "ELISA_RANDOM_SEED" and "random_seed" in value:
            raise ProfileConfigError(f"{configuration_id} cannot set both environment ELISA_RANDOM_SEED and random_seed")
        environment.append((name, _text(
            raw_value, f"{configuration_id}.environment.{name}",
            max_bytes=MAX_ENVIRONMENT_VALUE_BYTES, allow_empty=True,
        )))

    random_seed: int | None = None
    if "random_seed" in value:
        random_seed = _integer(value["random_seed"], f"{configuration_id}.random_seed", 0, (1 << 63) - 1)

    output_directory: Path | None = None
    if "output_directory" in value:
        output_text = _text(value["output_directory"], f"{configuration_id}.output_directory")
        output_directory = _resolve_path(output_text, root)

    return ProfileLaunchConfig(
        configuration_id=configuration_id,
        target=target,
        mode=mode,
        repetitions=repetitions,
        warmup=warmup,
        timeout_seconds=timeout,
        sample_period_us=sample_period,
        arguments=arguments,
        working_directory=working_directory,
        stdin=stdin,
        environment=tuple(sorted(environment)),
        random_seed=random_seed,
        output_directory=output_directory,
    )


def load_profile_configuration(source: Path, project_root: Path) -> ProfileLaunchConfig:
    """Load the selected config; an absent file keeps the initial safe defaults."""

    path = project_root / ".elisa-ide" / "profile-launches.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ProfileLaunchConfig(
            configuration_id="default",
            target=source,
            mode="functions",
            repetitions=1,
            warmup=0,
            timeout_seconds=30,
            sample_period_us=None,
            arguments=(),
            working_directory=project_root,
            stdin=None,
            environment=(),
            random_seed=None,
            output_directory=None,
        )
    except OSError as exc:
        raise ProfileConfigError(f"could not inspect profile configuration {path}: {exc}") from exc
    if path.is_symlink() or not path.is_file():
        raise ProfileConfigError(f"profile configuration must be a regular non-symlink file: {path}")
    if info.st_size <= 0 or info.st_size > MAX_CONFIG_BYTES:
        raise ProfileConfigError(f"profile configuration size must be between 1 and {MAX_CONFIG_BYTES} bytes")
    try:
        resolved_config = path.resolve(strict=True)
        resolved_config.relative_to(project_root.resolve(strict=True))
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except ProfileConfigError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProfileConfigError(f"profile configuration is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileConfigError("profile configuration root must be an object")
    _keys(value, {"schema_version", "active_configuration", "configurations"}, "profile configuration")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ProfileConfigError("profile configuration schema_version must be 1")
    active_id = _text(value.get("active_configuration"), "active_configuration", max_bytes=64)
    configurations = value.get("configurations")
    if not isinstance(configurations, list) or not 1 <= len(configurations) <= MAX_CONFIGURATIONS:
        raise ProfileConfigError(f"configurations must contain 1 through {MAX_CONFIGURATIONS} entries")
    parsed = [_parse_configuration(item, project_root) for item in configurations]
    by_id = {item.configuration_id: item for item in parsed}
    if len(by_id) != len(parsed):
        raise ProfileConfigError("configuration ids must be unique")
    selected = by_id.get(active_id)
    if selected is None:
        raise ProfileConfigError(f"active_configuration {active_id!r} does not match a configured id")
    if selected.target != source:
        raise ProfileConfigError(
            f"active configuration {active_id!r} targets {selected.target}, but the open source is {source}"
        )
    return selected
