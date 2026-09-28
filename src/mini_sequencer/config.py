"""Load and strictly validate YAML sequence configurations.

Validation happens entirely before any device connection. All problems found
are reported together, each prefixed with the path of the offending field
(for example ``steps[1].limits.min``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from mini_sequencer.models import (
    DeviceConfig,
    ExecutionConfig,
    Limits,
    SequenceConfig,
    StepConfig,
)

SUPPORTED_SCHEMA_VERSIONS = (1,)
SUPPORTED_STEP_COMMANDS = ("measure",)
STEP_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

_TOP_LEVEL_KEYS = {"schema_version", "name", "device", "execution", "steps"}
_DEVICE_KEYS = {"host", "port", "timeout_s"}
_EXECUTION_KEYS = {"stop_on_fail"}
_STEP_KEYS = {"id", "command", "channel", "unit", "limits"}
_LIMIT_KEYS = {"min", "max"}


class ConfigError(Exception):
    """Raised when a configuration cannot be loaded or fails validation."""

    def __init__(self, errors: list[str], source: str | None = None) -> None:
        self.errors = errors
        self.source = source
        heading = f"invalid configuration {source}" if source else "invalid configuration"
        super().__init__(heading + ":\n" + "\n".join(f"  - {e}" for e in errors))


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that rejects duplicate mapping keys instead of silently overwriting."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError as exc:
            raise ConfigError(
                [f"line {key_node.start_mark.line + 1}: mapping key must be a scalar"]
            ) from exc
        if duplicate:
            raise ConfigError([f"line {key_node.start_mark.line + 1}: duplicate key {key!r}"])
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_config(path: str | Path) -> SequenceConfig:
    """Read, parse, and validate a YAML sequence file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read file: {exc.strerror or exc}"], source=str(path)) from exc
    try:
        return parse_config(text)
    except ConfigError as exc:
        raise ConfigError(exc.errors, source=str(path)) from None


def parse_config(text: str) -> SequenceConfig:
    """Parse and validate YAML text."""
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError([f"{where}YAML syntax error: {problem}"]) from None
    return validate_config(data)


def validate_config(data: Any) -> SequenceConfig:
    """Validate an already-parsed configuration mapping."""
    v = _Validator()
    config = v.sequence(data)
    if v.errors or config is None:
        raise ConfigError(v.errors or ["configuration is empty"])
    return config


def config_to_dict(config: SequenceConfig) -> dict[str, Any]:
    data = asdict(config)
    data["steps"] = [asdict(step) for step in config.steps]
    return data


def config_snapshot(config: SequenceConfig) -> str:
    """Canonical JSON of the effective configuration, stored with every run."""
    return json.dumps(config_to_dict(config), sort_keys=True, separators=(",", ":"))


def config_hash(config: SequenceConfig) -> str:
    """SHA-256 of the test definition.

    Device host and port are excluded: they say where the unit is, not how it
    is judged, so moving a fixture to another port does not split summaries.
    The timeout, stop rule, steps, and limits are all included.
    """
    data = config_to_dict(config)
    data["device"] = {"timeout_s": config.device.timeout_s}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def mapping(self, value: Any, path: str, allowed: set[str], required: set[str]) -> bool:
        if not isinstance(value, dict):
            self.error(path, f"must be a mapping, got {type(value).__name__}")
            return False
        for key in value:
            if key not in allowed:
                self.error(
                    f"{path}.{key}" if path != "<root>" else str(key),
                    f"unknown field (allowed: {', '.join(sorted(allowed))})",
                )
        for key in sorted(required - set(value)):
            self.error(f"{path}.{key}" if path != "<root>" else key, "is required")
        return True

    def string(self, value: Any, path: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            self.error(path, "must be a non-empty string")
            return None
        return value

    def number(self, value: Any, path: str) -> float | None:
        if not _is_number(value):
            self.error(path, f"must be a number, got {type(value).__name__}")
            return None
        if not math.isfinite(value):
            self.error(path, "must be a finite number")
            return None
        return float(value)

    def sequence(self, data: Any) -> SequenceConfig | None:
        if data is None:
            self.error("<root>", "configuration is empty")
            return None
        if not self.mapping(data, "<root>", _TOP_LEVEL_KEYS, _TOP_LEVEL_KEYS - {"execution"}):
            return None

        schema_version = data.get("schema_version")
        if "schema_version" in data and (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version not in SUPPORTED_SCHEMA_VERSIONS
        ):
            self.error(
                "schema_version",
                f"unsupported value {schema_version!r} "
                f"(supported: {', '.join(map(str, SUPPORTED_SCHEMA_VERSIONS))})",
            )

        name = self.string(data.get("name"), "name") if "name" in data else None
        device = self.device(data["device"]) if "device" in data else None
        execution = self.execution(data.get("execution", {}))
        steps = self.steps(data["steps"]) if "steps" in data else None

        if self.errors or name is None or device is None or execution is None or steps is None:
            return None
        return SequenceConfig(
            schema_version=schema_version,
            name=name,
            device=device,
            execution=execution,
            steps=steps,
        )

    def device(self, data: Any) -> DeviceConfig | None:
        if not self.mapping(data, "device", _DEVICE_KEYS, {"host", "port"}):
            return None
        ok = True
        host = self.string(data.get("host"), "device.host") if "host" in data else None
        port = data.get("port")
        if "port" in data and (
            not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
        ):
            self.error("device.port", f"must be an integer from 1 to 65535, got {port!r}")
            ok = False
        timeout_s = 2.0
        if "timeout_s" in data:
            parsed = self.number(data["timeout_s"], "device.timeout_s")
            if parsed is None:
                ok = False
            elif parsed <= 0:
                self.error("device.timeout_s", f"must be greater than 0, got {parsed}")
                ok = False
            else:
                timeout_s = parsed
        if not ok or host is None or "port" not in data:
            return None
        return DeviceConfig(host=host, port=port, timeout_s=timeout_s)

    def execution(self, data: Any) -> ExecutionConfig | None:
        if data is None:
            data = {}
        if not self.mapping(data, "execution", _EXECUTION_KEYS, set()):
            return None
        stop_on_fail = data.get("stop_on_fail", False)
        if not isinstance(stop_on_fail, bool):
            self.error("execution.stop_on_fail", f"must be true or false, got {stop_on_fail!r}")
            return None
        return ExecutionConfig(stop_on_fail=stop_on_fail)

    def steps(self, data: Any) -> tuple[StepConfig, ...] | None:
        if not isinstance(data, list) or not data:
            self.error("steps", "must be a non-empty list")
            return None
        steps: list[StepConfig] = []
        seen: dict[str, int] = {}
        for index, item in enumerate(data):
            path = f"steps[{index}]"
            step = self.step(item, path)
            if step is None:
                continue
            if step.id in seen:
                self.error(
                    f"{path}.id", f"duplicate step id {step.id!r} (also steps[{seen[step.id]}])"
                )
                continue
            seen[step.id] = index
            steps.append(step)
        if len(steps) != len(data):
            return None
        return tuple(steps)

    def step(self, data: Any, path: str) -> StepConfig | None:
        if not self.mapping(data, path, _STEP_KEYS, _STEP_KEYS):
            return None
        step_id = data.get("id")
        if "id" in data and (not isinstance(step_id, str) or not STEP_ID_PATTERN.match(step_id)):
            self.error(
                f"{path}.id",
                "must start with a letter and contain only letters, digits, '_', '.', '-' "
                f"(max 64 chars), got {step_id!r}",
            )
            step_id = None
        command = data.get("command")
        if "command" in data and command not in SUPPORTED_STEP_COMMANDS:
            supported = ", ".join(SUPPORTED_STEP_COMMANDS)
            self.error(
                f"{path}.command", f"unsupported command {command!r} (supported: {supported})"
            )
            command = None
        channel = self.string(data["channel"], f"{path}.channel") if "channel" in data else None
        unit = self.string(data["unit"], f"{path}.unit") if "unit" in data else None
        limits = self.limits(data["limits"], f"{path}.limits") if "limits" in data else None
        if None in (step_id, command, channel, unit, limits):
            return None
        return StepConfig(id=step_id, command=command, channel=channel, unit=unit, limits=limits)

    def limits(self, data: Any, path: str) -> Limits | None:
        if not self.mapping(data, path, _LIMIT_KEYS, set()):
            return None
        if not any(key in data for key in _LIMIT_KEYS):
            self.error(path, "must define at least one of 'min' or 'max'")
            return None
        low = self.number(data["min"], f"{path}.min") if "min" in data else None
        high = self.number(data["max"], f"{path}.max") if "max" in data else None
        if ("min" in data and low is None) or ("max" in data and high is None):
            return None
        if low is not None and high is not None and low > high:
            self.error(path, f"min ({low}) is greater than max ({high})")
            return None
        return Limits(min=low, max=high)
