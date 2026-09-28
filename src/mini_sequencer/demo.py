"""Seeded batch demonstration: many units against an isolated in-process simulator."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from mini_sequencer.config import parse_config
from mini_sequencer.models import RunRecord, SequenceConfig
from mini_sequencer.sequencer import run_sequence
from mini_sequencer.simulator import DeviceSimulator
from mini_sequencer.storage import ResultStore

# Same content as examples/sequence.yaml, bundled so the installed package can run the demo.
DEFAULT_SEQUENCE_YAML = """\
schema_version: 1
name: basic_device_check
device:
  host: 127.0.0.1
  port: 9000
  timeout_s: 2.0
execution:
  stop_on_fail: false
steps:
  - id: supply_voltage
    command: measure
    channel: voltage
    unit: V
    limits:
      min: 4.75
      max: 5.25
  - id: idle_current
    command: measure
    channel: current
    unit: mA
    limits:
      max: 120.0
  - id: temperature
    command: measure
    channel: temperature
    unit: C
    limits:
      min: 15.0
      max: 45.0
"""

# Share of units per simulator profile, out of 100.
DEMO_MIX: tuple[tuple[str, int], ...] = (
    ("nominal", 82),
    ("undervoltage", 6),
    ("overcurrent", 5),
    ("overtemperature", 3),
    ("timeout", 2),
    ("malformed", 2),
)


@dataclass(frozen=True)
class DemoUnit:
    index: int
    serial: str
    profile: str
    seed: int


def default_config() -> SequenceConfig:
    return parse_config(DEFAULT_SEQUENCE_YAML)


def profile_counts(units: int) -> dict[str, int]:
    """Split ``units`` across :data:`DEMO_MIX` by largest remainder (exact, deterministic)."""
    if units < 1:
        raise ValueError("units must be at least 1")
    total_weight = sum(w for _, w in DEMO_MIX)
    quotas = [(name, units * w / total_weight) for name, w in DEMO_MIX]
    counts = {name: int(q) for name, q in quotas}
    leftover = units - sum(counts.values())
    by_remainder = sorted(
        range(len(quotas)), key=lambda i: (-(quotas[i][1] - int(quotas[i][1])), i)
    )
    for i in by_remainder[:leftover]:
        counts[quotas[i][0]] += 1
    return counts


def plan_units(units: int, seed: int) -> list[DemoUnit]:
    """Deterministic, shuffled assignment of profiles and measurement seeds to units."""
    rng = random.Random(seed)
    profiles = [name for name, n in profile_counts(units).items() for _ in range(n)]
    rng.shuffle(profiles)
    return [
        DemoUnit(
            index=i,
            serial=f"DEMO-{seed}-{i:04d}",
            profile=profile,
            seed=rng.randrange(2**32),
        )
        for i, profile in enumerate(profiles, start=1)
    ]


def run_demo(
    store: ResultStore,
    units: int,
    seed: int,
    config: SequenceConfig,
    timeout_s: float,
    on_unit: Callable[[DemoUnit, RunRecord], None] | None = None,
) -> list[RunRecord]:
    """Start a simulator on a free loopback port, run every unit, then stop it."""
    plan = plan_units(units, seed)
    runs: list[RunRecord] = []
    with DeviceSimulator(host="127.0.0.1", port=0) as simulator:
        host, port = simulator.address
        effective = config.with_device(host=host, port=port, timeout_s=timeout_s)
        for unit in plan:
            simulator.configure(unit.profile, unit.seed)
            run = run_sequence(effective, unit.serial, store)
            runs.append(run)
            if on_unit is not None:
                on_unit(unit, run)
    return runs
