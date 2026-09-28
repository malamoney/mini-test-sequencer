"""The seeded demo must agree with counts calculated independently of the database."""

from __future__ import annotations

from collections import Counter

from mini_sequencer.cli import EXIT_PASS, main
from mini_sequencer.demo import default_config, plan_units, run_demo
from mini_sequencer.models import RunStatus
from mini_sequencer.storage import SummaryFilter

# Expected outcome of each simulator profile under the example limits.
EXPECTED_BY_PROFILE = {
    "nominal": (RunStatus.PASS, None, None),
    "undervoltage": (RunStatus.FAIL, "supply_voltage", None),
    "overcurrent": (RunStatus.FAIL, "idle_current", None),
    "overtemperature": (RunStatus.FAIL, "temperature", None),
    "timeout": (RunStatus.ERROR, None, "timeout"),
    "malformed": (RunStatus.ERROR, None, "protocol"),
}


def expected_totals(units: int, seed: int):
    statuses, failures, errors = Counter(), Counter(), Counter()
    for unit in plan_units(units, seed):
        status, failing_step, error = EXPECTED_BY_PROFILE[unit.profile]
        statuses[status] += 1
        if failing_step:
            failures[failing_step] += 1
        if error:
            errors[error] += 1
    return statuses, failures, errors


def test_demo_batch_matches_fixture_expectations(store):
    units, seed = 60, 42
    runs = run_demo(store, units, seed, default_config(), timeout_s=0.2)
    statuses, failures, errors = expected_totals(units, seed)

    # Per-unit: each run's outcome follows from its planned profile.
    for unit, run in zip(plan_units(units, seed), runs, strict=True):
        assert run.serial == unit.serial
        assert run.status is EXPECTED_BY_PROFILE[unit.profile][0]
        assert run.device_identity["profile"] == unit.profile

    (group,) = store.summarize(SummaryFilter())
    assert {s: n for s, n in group.status_counts.items() if n} == dict(statuses)
    assert group.failures_by_step == sorted(failures.items(), key=lambda kv: (-kv[1], kv[0]))
    assert dict(group.errors_by_category) == dict(errors)


def test_demo_100_units_seed_42_published_numbers(tmp_path, capsys):
    """The README sample output comes from exactly this command."""
    db = tmp_path / "demo.sqlite"
    assert (
        main(["demo", "--units", "100", "--seed", "42", "--db", str(db), "--timeout", "0.2"])
        == EXIT_PASS
    )
    out = capsys.readouterr().out
    assert "Completed runs: 100  (PASS 82, FAIL 14, ERROR 4)" in out
    assert "Run yield:      82.0%" in out
    lines = [line.split() for line in out.splitlines()]
    pareto = [
        row for row in lines if row and row[0] in {"supply_voltage", "idle_current", "temperature"}
    ]
    assert pareto == [
        ["supply_voltage", "6", "42.9%", "42.9%"],
        ["idle_current", "5", "35.7%", "78.6%"],
        ["temperature", "3", "21.4%", "100.0%"],
    ]


def test_demo_measurements_repeat_for_same_seed(tmp_path):
    from mini_sequencer.storage import SQLiteResultStore

    values = []
    for name in ("a", "b"):
        with SQLiteResultStore(tmp_path / f"{name}.sqlite") as store:
            runs = run_demo(store, 10, 7, default_config(), timeout_s=0.2)
            values.append([[s.measurement for s in r.steps] for r in runs])
    assert values[0] == values[1]
