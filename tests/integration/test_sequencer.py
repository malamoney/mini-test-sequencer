from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from mini_sequencer.config import config_hash
from mini_sequencer.models import ExecutionConfig, Measurement, RunStatus, StepStatus
from mini_sequencer.sequencer import run_sequence

P, F, E, S = StepStatus.PASS, StepStatus.FAIL, StepStatus.ERROR, StepStatus.SKIP


def statuses(run):
    return [s.status for s in run.steps]


def test_nominal_run_passes_and_is_persisted(simulator, sim_config, store):
    run = run_sequence(sim_config, "SN-1", store)
    assert run.status is RunStatus.PASS
    assert statuses(run) == [P, P, P]

    stored = store.get_run(run.id)
    assert stored.status is RunStatus.PASS
    assert stored.serial == "SN-1"
    assert stored.device_identity["model"] == "MTS-SIM-1"
    assert stored.config_hash == config_hash(sim_config)
    assert json.loads(stored.config_snapshot)["steps"][0]["id"] == "supply_voltage"
    assert stored.ended_at is not None and stored.duration_s >= 0
    assert [s.measurement for s in stored.steps] == [s.measurement for s in run.steps]
    assert stored.steps[0].limit_min == 4.75 and stored.steps[0].limit_max == 5.25


def test_limit_failure_continues_by_default(simulator, sim_config, store):
    simulator.configure("undervoltage", seed=3)
    run = run_sequence(sim_config, "SN-2", store)
    assert run.status is RunStatus.FAIL
    assert statuses(run) == [F, P, P]


def test_stop_on_fail_skips_remaining(simulator, sim_config, store):
    simulator.configure("undervoltage", seed=3)
    config = replace(sim_config, execution=ExecutionConfig(stop_on_fail=True))
    run = run_sequence(config, "SN-3", store)
    assert run.status is RunStatus.FAIL
    assert statuses(run) == [F, S, S]
    assert [s.status for s in store.get_run(run.id).steps] == [F, S, S]


def test_timeout_error_stops_and_keeps_earlier_results(simulator, sim_config, store):
    simulator.configure("timeout", seed=3)  # stalls on the third step
    run = run_sequence(sim_config.with_device(timeout_s=0.2), "SN-4", store)
    assert run.status is RunStatus.ERROR
    assert statuses(run) == [P, P, E]
    stored = store.get_run(run.id)
    assert stored.error_category == "timeout"
    assert stored.steps[0].measurement is not None  # retained progress
    assert stored.steps[2].error_category == "timeout"


def test_protocol_error_skips_rest(simulator, sim_config, store):
    simulator.configure("malformed", seed=3)  # garbles the second step
    run = run_sequence(sim_config, "SN-5", store)
    assert statuses(run) == [P, E, S]
    assert run.error_category == "protocol"


def test_failure_then_error_is_error(sim_config, store):
    measured = []

    @contextmanager
    def factory(config):
        class Device:
            def identify(self):
                return {"model": "fake", "firmware": "0"}

            def reset(self):
                pass

            def measure(self, channel):
                measured.append(channel)
                if channel == "voltage":
                    return Measurement(1.0, "V")
                raise ConnectionResetError("boom")

        yield Device()

    run = run_sequence(sim_config, "SN-6", store, device_factory=factory)
    assert statuses(run) == [F, E, S]
    assert run.status is RunStatus.ERROR
    assert run.steps[1].error_category == "internal"
    assert measured == ["voltage", "current"]


def test_unit_mismatch_is_error_not_failure(sim_config, store):
    @contextmanager
    def factory(config):
        class Device:
            def identify(self):
                return {"model": "fake", "firmware": "0"}

            def reset(self):
                pass

            def measure(self, channel):
                return Measurement(5000.0, "mV")

        yield Device()

    run = run_sequence(sim_config, "SN-7", store, device_factory=factory)
    assert statuses(run) == [E, S, S]
    step = store.get_run(run.id).steps[0]
    assert step.error_category == "unit_mismatch"
    assert step.error_detail == {"expected_unit": "V", "received_unit": "mV"}
    assert step.measurement == 5000.0


def test_unreachable_device_is_error_with_all_steps_skipped(sim_config, store, simulator):
    simulator.stop()
    run = run_sequence(sim_config, "SN-8", store)
    assert run.status is RunStatus.ERROR
    assert run.error_category in {"connection", "timeout"}
    assert statuses(run) == [S, S, S]


def test_keyboard_interrupt_records_aborted(sim_config, store):
    @contextmanager
    def factory(config):
        class Device:
            def identify(self):
                return {"model": "fake", "firmware": "0"}

            def reset(self):
                pass

            def measure(self, channel):
                if channel == "current":
                    raise KeyboardInterrupt
                return Measurement(5.0, "V")

        yield Device()

    with pytest.raises(KeyboardInterrupt):
        run_sequence(sim_config, "SN-9", store, device_factory=factory)
    (run,) = store.list_runs()
    assert run.status is RunStatus.ABORTED
    assert [s.status for s in store.get_run(run.id).steps] == [P]


def test_repeated_serials_are_separate_runs(simulator, sim_config, store):
    a = run_sequence(sim_config, "SN-X", store)
    b = run_sequence(sim_config, "SN-X", store)
    assert a.id != b.id
    assert len(store.list_runs()) == 2
