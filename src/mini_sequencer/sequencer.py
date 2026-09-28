"""Execute a validated sequence against one device and persist the outcome.

Outcome rules (see ``docs/design.md``):

* ``PASS`` / ``FAIL``: a valid measurement met / violated its inclusive limits.
* ``ERROR``: communication, protocol, or execution failed. The sequence stops,
  because device state is uncertain.
* ``SKIP``: not attempted after a stop condition (an error, or a failure with
  ``stop_on_fail``).

Run status precedence is ERROR, then FAIL, then PASS. A ``KeyboardInterrupt``
finalizes the run as ABORTED and is re-raised.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Protocol

from mini_sequencer import __version__
from mini_sequencer.client import DeviceClient, DeviceError
from mini_sequencer.config import config_hash, config_snapshot
from mini_sequencer.models import (
    Limits,
    Measurement,
    RunRecord,
    RunStatus,
    SequenceConfig,
    StepConfig,
    StepRecord,
    StepStatus,
)
from mini_sequencer.storage import ResultStore


class Device(Protocol):
    def identify(self) -> dict[str, Any]: ...
    def reset(self) -> None: ...
    def measure(self, channel: str) -> Measurement: ...


DeviceFactory = Callable[[SequenceConfig], AbstractContextManager[Device]]


def default_device_factory(config: SequenceConfig) -> DeviceClient:
    return DeviceClient(config.device.host, config.device.port, config.device.timeout_s)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def evaluate(value: float, limits: Limits) -> StepStatus:
    """Inclusive limit check on the unrounded value."""
    return StepStatus.PASS if limits.contains(value) else StepStatus.FAIL


def aggregate_status(step_statuses: Iterable[StepStatus]) -> RunStatus:
    """Combine step outcomes: any ERROR, else any FAIL, else PASS."""
    statuses = set(step_statuses)
    if StepStatus.ERROR in statuses:
        return RunStatus.ERROR
    if StepStatus.FAIL in statuses:
        return RunStatus.FAIL
    return RunStatus.PASS


class _StepError(Exception):
    def __init__(self, category: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.category = category
        self.detail = detail


def run_sequence(
    config: SequenceConfig,
    serial: str,
    store: ResultStore,
    device_factory: DeviceFactory = default_device_factory,
    on_step: Callable[[StepRecord], None] | None = None,
) -> RunRecord:
    """Run every step of ``config`` against the device and return the stored run."""
    run = RunRecord(
        id=str(uuid.uuid4()),
        serial=serial,
        sequence_name=config.name,
        config_hash=config_hash(config),
        config_snapshot=config_snapshot(config),
        package_version=__version__,
        started_at=utc_now(),
        status=RunStatus.RUNNING,
    )
    store.create_run(run)
    started = time.monotonic()
    run_error: tuple[str, str] | None = None
    next_position = 1

    def record(step: StepRecord) -> None:
        store.save_step(run.id, step)
        run.steps.append(step)
        if on_step is not None:
            on_step(step)

    def skip_remaining() -> None:
        for position, step in enumerate(config.steps[next_position - 1 :], start=next_position):
            record(_blank_step(position, step, StepStatus.SKIP))

    try:
        try:
            with device_factory(config) as device:
                identity = device.identify()
                run.device_identity = identity
                store.set_device_identity(run.id, identity)
                device.reset()

                for position, step in enumerate(config.steps, start=1):
                    next_position = position
                    result = _execute_step(device, position, step)
                    record(result)
                    next_position = position + 1
                    if result.status is StepStatus.ERROR:
                        run_error = (result.error_category or "unknown", result.error_message or "")
                        break
                    if result.status is StepStatus.FAIL and config.execution.stop_on_fail:
                        break
        except DeviceError as exc:
            # Connecting, identify, or reset failed: no step was attempted.
            run_error = (exc.category, f"device setup failed: {exc}")
        skip_remaining()
        status = RunStatus.ERROR if run_error else aggregate_status(s.status for s in run.steps)
    except KeyboardInterrupt:
        run.status = RunStatus.ABORTED
        _finalize(store, run, started, ("aborted", "interrupted by user"))
        raise

    run.status = status
    _finalize(store, run, started, run_error)
    return run


def _finalize(
    store: ResultStore, run: RunRecord, started: float, error: tuple[str, str] | None
) -> None:
    run.ended_at = utc_now()
    run.duration_s = time.monotonic() - started
    run.error_category, run.error_message = error if error else (None, None)
    store.finalize_run(
        run.id, run.status, run.ended_at, run.duration_s, run.error_category, run.error_message
    )


def _blank_step(position: int, step: StepConfig, status: StepStatus) -> StepRecord:
    return StepRecord(
        position=position,
        step_id=step.id,
        command=step.command,
        channel=step.channel,
        status=status,
        unit=step.unit,
        limit_min=step.limits.min,
        limit_max=step.limits.max,
    )


def _execute_step(device: Device, position: int, step: StepConfig) -> StepRecord:
    result = _blank_step(position, step, StepStatus.ERROR)
    result.started_at = utc_now()
    started = time.monotonic()
    try:
        measurement = device.measure(step.channel)
        result.measurement = measurement.value
        result.measured_unit = measurement.unit
        if measurement.unit != step.unit:
            raise _StepError(
                "unit_mismatch",
                f"device reported {measurement.unit!r}, sequence expects {step.unit!r}",
                {"expected_unit": step.unit, "received_unit": measurement.unit},
            )
        result.status = evaluate(measurement.value, step.limits)
    except DeviceError as exc:
        result.error_category = exc.category
        result.error_message = str(exc)
        result.error_detail = exc.detail or None
    except _StepError as exc:
        result.error_category = exc.category
        result.error_message = str(exc)
        result.error_detail = exc.detail
    except Exception as exc:  # noqa: BLE001 - recorded, then the sequence stops
        result.error_category = "internal"
        result.error_message = f"{type(exc).__name__}: {exc}"
    result.ended_at = utc_now()
    result.duration_s = time.monotonic() - started
    return result
