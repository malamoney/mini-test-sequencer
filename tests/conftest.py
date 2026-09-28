from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mini_sequencer.demo import default_config
from mini_sequencer.models import SequenceConfig
from mini_sequencer.simulator import DeviceSimulator
from mini_sequencer.storage import SQLiteResultStore

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "sequence.yaml"


@pytest.fixture
def simulator() -> Iterator[DeviceSimulator]:
    with DeviceSimulator(host="127.0.0.1", port=0, profile="nominal", seed=1) as sim:
        yield sim


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteResultStore]:
    with SQLiteResultStore(tmp_path / "results.sqlite") as s:
        yield s


@pytest.fixture
def sim_config(simulator: DeviceSimulator) -> SequenceConfig:
    """The example sequence pointed at the test simulator, with a short timeout."""
    host, port = simulator.address
    return default_config().with_device(host=host, port=port, timeout_s=0.5)
