from __future__ import annotations

import textwrap

import pytest

from mini_sequencer.config import ConfigError, config_hash, load_config, parse_config
from mini_sequencer.demo import DEFAULT_SEQUENCE_YAML
from tests.conftest import EXAMPLE_CONFIG

VALID = textwrap.dedent(
    """\
    schema_version: 1
    name: seq
    device: {host: 127.0.0.1, port: 9000, timeout_s: 1.5}
    steps:
      - id: v
        command: measure
        channel: voltage
        unit: V
        limits: {min: 4.75, max: 5.25}
    """
)


def errors_for(text: str) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse_config(textwrap.dedent(text))
    return info.value.errors


def test_example_file_is_valid_and_matches_bundled_default():
    example = load_config(EXAMPLE_CONFIG)
    assert example == parse_config(DEFAULT_SEQUENCE_YAML)
    assert [s.id for s in example.steps] == ["supply_voltage", "idle_current", "temperature"]
    assert example.steps[1].limits.min is None
    assert example.steps[1].limits.max == 120.0


def test_valid_config_defaults():
    config = parse_config(VALID)
    assert config.execution.stop_on_fail is False
    assert config.device.timeout_s == 1.5
    assert config.steps[0].limits.min == 4.75


def test_timeout_defaults_when_omitted():
    config = parse_config(VALID.replace(", timeout_s: 1.5", ""))
    assert config.device.timeout_s == 2.0


def test_duplicate_keys_rejected():
    errors = errors_for(VALID + "name: other\n")
    assert any("duplicate key 'name'" in e for e in errors)


def test_duplicate_nested_keys_rejected():
    errors = errors_for(VALID.replace("{min: 4.75, max: 5.25}", "{min: 4.75, min: 5.0}"))
    assert any("duplicate key 'min'" in e for e in errors)


def test_unknown_fields_rejected():
    errors = errors_for(VALID.replace("unit: V", "unit: V\n    retries: 3"))
    assert errors == [
        "steps[0].retries: unknown field (allowed: channel, command, id, limits, unit)"
    ]


def test_unknown_top_level_field_rejected():
    assert "extra: unknown field" in errors_for(VALID + "extra: 1\n")[0]


def test_duplicate_step_ids_rejected():
    text = VALID + textwrap.indent(
        "- id: v\n  command: measure\n  channel: current\n  unit: mA\n  limits: {max: 1}\n", "  "
    )
    errors = errors_for(text)
    assert errors == ["steps[1].id: duplicate step id 'v' (also steps[0])"]


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ("{}", "must define at least one of 'min' or 'max'"),
        ("{min: 5.25, max: 4.75}", "min (5.25) is greater than max (4.75)"),
        ("{min: .nan}", "must be a finite number"),
        ("{max: .inf}", "must be a finite number"),
        ("{min: true}", "must be a number, got bool"),
        ("{min: yes}", "must be a number, got bool"),
        ("{min: '4.75'}", "must be a number, got str"),
        ("{min: ~}", "must be a number, got NoneType"),
    ],
)
def test_bad_limits_rejected(limits: str, message: str):
    errors = errors_for(VALID.replace("{min: 4.75, max: 5.25}", limits))
    assert len(errors) == 1
    assert message in errors[0]
    assert errors[0].startswith("steps[0].limits")


def test_equal_min_and_max_allowed():
    config = parse_config(VALID.replace("{min: 4.75, max: 5.25}", "{min: 5, max: 5}"))
    assert config.steps[0].limits.min == config.steps[0].limits.max == 5.0


def test_missing_limits_rejected():
    errors = errors_for(VALID.replace("    limits: {min: 4.75, max: 5.25}\n", ""))
    assert errors == ["steps[0].limits: is required"]


@pytest.mark.parametrize("port", ["0", "65536", "true", "'9000'", "90.0"])
def test_invalid_ports_rejected(port: str):
    errors = errors_for(VALID.replace("port: 9000", f"port: {port}"))
    assert errors[0].startswith("device.port: must be an integer from 1 to 65535")


@pytest.mark.parametrize("timeout", ["0", "-1", ".inf", "false"])
def test_nonpositive_or_invalid_timeouts_rejected(timeout: str):
    errors = errors_for(VALID.replace("timeout_s: 1.5", f"timeout_s: {timeout}"))
    assert errors[0].startswith("device.timeout_s:")


def test_stop_on_fail_must_be_boolean():
    errors = errors_for(VALID + "execution: {stop_on_fail: 'yes please'}\n")
    assert errors[0].startswith("execution.stop_on_fail: must be true or false")


def test_unsupported_command_and_schema_version():
    errors = errors_for(
        VALID.replace("command: measure", "command: rm").replace(
            "schema_version: 1", "schema_version: 2"
        )
    )
    assert any(e.startswith("schema_version: unsupported value 2") for e in errors)
    assert any(e.startswith("steps[0].command: unsupported command 'rm'") for e in errors)


def test_multiple_errors_reported_together():
    errors = errors_for(
        VALID.replace("port: 9000", "port: 0").replace("{min: 4.75, max: 5.25}", "{}")
    )
    assert len(errors) == 2


@pytest.mark.parametrize("text", ["", "[]", "just a string", "steps: [\n"])
def test_non_mapping_or_broken_yaml_rejected(text: str):
    with pytest.raises(ConfigError):
        parse_config(text)


def test_yaml_syntax_error_has_line_number():
    errors = errors_for("name: x\nsteps: [\n  - {id: a\n")
    assert "YAML syntax error" in errors[0]
    assert errors[0].startswith("line ")


def test_unsafe_yaml_tags_rejected():
    with pytest.raises(ConfigError):
        parse_config("!!python/object/apply:os.system ['echo hi']")


def test_load_config_reports_missing_file(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path / "nope.yaml")
    assert "cannot read file" in info.value.errors[0]
    assert "nope.yaml" in str(info.value)


def test_config_hash_ignores_host_and_port_but_not_limits():
    config = parse_config(VALID)
    assert config_hash(config) == config_hash(config.with_device(host="10.0.0.1", port=1234))
    assert config_hash(config) != config_hash(config.with_device(timeout_s=9.0))
    changed = parse_config(VALID.replace("max: 5.25", "max: 5.3"))
    assert config_hash(config) != config_hash(changed)
