from __future__ import annotations

import pytest

from mini_sequencer.models import RunStatus
from mini_sequencer.reporting import format_group, format_value, pareto, run_yield
from mini_sequencer.storage import GroupSummary


def test_run_yield():
    assert run_yield(82, 100) == 82.0
    assert run_yield(0, 3) == 0.0
    assert run_yield(0, 0) is None


def test_run_yield_rejects_impossible_counts():
    with pytest.raises(ValueError):
        run_yield(5, 4)


def test_pareto_orders_and_accumulates():
    rows = pareto([("temperature", 3), ("idle_current", 5), ("supply_voltage", 6)])
    assert [r.step_id for r in rows] == ["supply_voltage", "idle_current", "temperature"]
    assert [round(r.percent, 1) for r in rows] == [42.9, 35.7, 21.4]
    assert [round(r.cumulative_percent, 1) for r in rows] == [42.9, 78.6, 100.0]


def test_pareto_ties_broken_by_step_id_and_zeros_dropped():
    rows = pareto([("b", 2), ("a", 2), ("c", 0)])
    assert [r.step_id for r in rows] == ["a", "b"]
    assert pareto([]) == []


def test_format_value_rounds_only_for_display():
    assert format_value(5.0) == "5"
    assert format_value(4.591823) == "4.5918"
    assert format_value(-0.00001) == "0"
    assert format_value(None) == "-"


def _group(**counts) -> GroupSummary:
    status_counts = {s: 0 for s in RunStatus}
    status_counts.update({RunStatus[k]: v for k, v in counts.items()})
    return GroupSummary("seq", "a" * 64, status_counts)


def test_group_with_only_unfinished_runs_shows_na_and_empty_states():
    text = "\n".join(format_group(_group(RUNNING=2, ABORTED=1)))
    assert "Completed runs: 0" in text
    assert "Run yield:      N/A (no completed runs)" in text
    assert "ABORTED 1, unfinished RUNNING 2" in text
    assert "No limit failures recorded." in text
    assert "No execution errors recorded." in text


def test_group_yield_excludes_aborted_and_running():
    text = "\n".join(format_group(_group(PASS=3, FAIL=1, ABORTED=5, RUNNING=5)))
    assert "Completed runs: 4" in text
    assert "Run yield:      75.0%" in text
