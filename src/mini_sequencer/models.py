"""Typed domain models shared by configuration, sequencing, storage, and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIP = "SKIP"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    ABORTED = "ABORTED"


COMPLETED_RUN_STATUSES = (RunStatus.PASS, RunStatus.FAIL, RunStatus.ERROR)


@dataclass(frozen=True)
class Limits:
    """Inclusive bounds. At least one of ``min``/``max`` is set."""

    min: float | None = None
    max: float | None = None

    def contains(self, value: float) -> bool:
        if self.min is not None and value < self.min:
            return False
        return not (self.max is not None and value > self.max)


@dataclass(frozen=True)
class DeviceConfig:
    host: str
    port: int
    timeout_s: float = 2.0


@dataclass(frozen=True)
class ExecutionConfig:
    stop_on_fail: bool = False


@dataclass(frozen=True)
class StepConfig:
    id: str
    command: str
    channel: str
    unit: str
    limits: Limits


@dataclass(frozen=True)
class SequenceConfig:
    schema_version: int
    name: str
    device: DeviceConfig
    execution: ExecutionConfig
    steps: tuple[StepConfig, ...]

    def with_device(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout_s: float | None = None,
    ) -> SequenceConfig:
        """Return a copy with selected device connection settings overridden."""
        device = replace(
            self.device,
            host=self.device.host if host is None else host,
            port=self.device.port if port is None else port,
            timeout_s=self.device.timeout_s if timeout_s is None else timeout_s,
        )
        return replace(self, device=device)


@dataclass(frozen=True)
class Measurement:
    value: float
    unit: str


@dataclass
class StepRecord:
    position: int
    step_id: str
    command: str
    channel: str
    status: StepStatus
    unit: str
    limit_min: float | None
    limit_max: float | None
    measurement: float | None = None
    measured_unit: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    error_category: str | None = None
    error_message: str | None = None
    error_detail: dict[str, Any] | None = None


@dataclass
class RunRecord:
    id: str
    serial: str
    sequence_name: str
    config_hash: str
    config_snapshot: str
    package_version: str
    started_at: str
    status: RunStatus
    ended_at: str | None = None
    duration_s: float | None = None
    device_identity: dict[str, Any] | None = None
    error_category: str | None = None
    error_message: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
