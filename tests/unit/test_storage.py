from __future__ import annotations

import sqlite3

import pytest

from mini_sequencer.models import RunRecord, RunStatus, StepRecord, StepStatus
from mini_sequencer.storage import SCHEMA_VERSION, SQLiteResultStore, StorageError, SummaryFilter


def make_run(
    run_id: str,
    *,
    started_at: str = "2026-01-01T00:00:00.000000+00:00",
    config_hash: str = "h1",
    name: str = "seq",
    serial: str = "SN1",
) -> RunRecord:
    return RunRecord(
        id=run_id,
        serial=serial,
        sequence_name=name,
        config_hash=config_hash,
        config_snapshot="{}",
        package_version="test",
        started_at=started_at,
        status=RunStatus.RUNNING,
    )


def make_step(position: int, step_id: str, status: StepStatus) -> StepRecord:
    return StepRecord(
        position=position,
        step_id=step_id,
        command="measure",
        channel="voltage",
        status=status,
        unit="V",
        limit_min=1.0,
        limit_max=2.0,
        measurement=1.5 if status is not StepStatus.SKIP else None,
    )


def finished(store, run_id, status, steps=(), error=None, **kw):
    store.create_run(make_run(run_id, **kw))
    for step in steps:
        store.save_step(run_id, step)
    store.finalize_run(run_id, status, "2026-01-01T00:00:01+00:00", 1.0, *(error or (None, None)))


def test_schema_version_and_foreign_keys(store):
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.save_step("missing-run", make_step(1, "a", StepStatus.PASS))


def test_reopening_existing_database(tmp_path):
    path = tmp_path / "r.sqlite"
    with SQLiteResultStore(path) as s:
        finished(s, "run-1", RunStatus.PASS)
    with SQLiteResultStore(path) as s:
        assert s.get_run("run-1").status is RunStatus.PASS


def test_rejects_foreign_database(tmp_path):
    path = tmp_path / "other.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE something (x)")
    conn.commit()
    conn.close()
    with pytest.raises(StorageError, match="not a mini-sequencer result database"):
        SQLiteResultStore(path)


def test_rejects_newer_schema(tmp_path):
    path = tmp_path / "new.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(StorageError, match="schema version 99"):
        SQLiteResultStore(path)


def test_one_result_per_step_position(store):
    store.create_run(make_run("r"))
    store.save_step("r", make_step(1, "a", StepStatus.PASS))
    with pytest.raises(sqlite3.IntegrityError):
        store.save_step("r", make_step(1, "b", StepStatus.PASS))


def test_round_trip_run_and_steps(store):
    step = make_step(1, "a", StepStatus.ERROR)
    step.error_category = "unit_mismatch"
    step.error_detail = {"expected_unit": "V", "received_unit": "mV"}
    finished(store, "abc-123", RunStatus.ERROR, [step], ("unit_mismatch", "bad unit"))
    store.set_device_identity("abc-123", {"model": "M"})
    run = store.get_run("abc")  # prefix lookup
    assert run.id == "abc-123"
    assert run.device_identity == {"model": "M"}
    assert run.error_category == "unit_mismatch"
    assert run.steps[0].error_detail == {"expected_unit": "V", "received_unit": "mV"}


def test_prefix_lookup_is_literal_and_rejects_ambiguity(store):
    finished(store, "ab_1", RunStatus.PASS)
    finished(store, "ab_2", RunStatus.PASS)
    assert store.get_run("a%") is None
    with pytest.raises(StorageError, match="more than one"):
        store.get_run("ab_")


def test_finalize_only_once(store):
    finished(store, "r", RunStatus.PASS)
    with pytest.raises(StorageError):
        store.finalize_run("r", RunStatus.FAIL, "t", 1.0)


def test_abort_stale_run(store):
    store.create_run(make_run("stale-run"))
    store.save_step("stale-run", make_step(1, "a", StepStatus.PASS))
    run = store.abort_stale_run("stale", "2026-01-02T00:00:00+00:00", "operator cleanup")
    assert run.status is RunStatus.ABORTED
    assert run.error_message == "operator cleanup"
    assert len(run.steps) == 1  # completed progress is kept


def test_abort_refuses_finished_or_unknown_runs(store):
    finished(store, "done", RunStatus.PASS)
    with pytest.raises(StorageError, match="not RUNNING"):
        store.abort_stale_run("done", "t", "x")
    with pytest.raises(StorageError, match="no run matches"):
        store.abort_stale_run("zzz", "t", "x")


def test_summary_denominators_and_grouping(store):
    P, F, E = StepStatus.PASS, StepStatus.FAIL, StepStatus.ERROR
    finished(store, "p1", RunStatus.PASS, [make_step(1, "a", P), make_step(2, "b", P)])
    finished(store, "f1", RunStatus.FAIL, [make_step(1, "a", F), make_step(2, "b", F)])
    finished(store, "f2", RunStatus.FAIL, [make_step(1, "a", F), make_step(2, "b", P)])
    finished(store, "e1", RunStatus.ERROR, [make_step(1, "a", E)], ("timeout", "slow"))
    finished(store, "x1", RunStatus.ABORTED)
    # An unfinished run with a FAIL step must not add to the Pareto.
    store.create_run(make_run("u1"))
    store.save_step("u1", make_step(1, "a", F))
    # Different limits -> separate group.
    finished(store, "other", RunStatus.FAIL, [make_step(1, "a", F)], config_hash="h2")

    groups = {g.config_hash: g for g in store.summarize(SummaryFilter())}
    g = groups["h1"]
    assert g.status_counts[RunStatus.PASS] == 1
    assert g.status_counts[RunStatus.FAIL] == 2
    assert g.status_counts[RunStatus.ERROR] == 1
    assert g.status_counts[RunStatus.ABORTED] == 1
    assert g.status_counts[RunStatus.RUNNING] == 1
    assert g.failures_by_step == [("a", 2), ("b", 1)]
    assert g.errors_by_category == [("timeout", 1)]
    assert groups["h2"].failures_by_step == [("a", 1)]


def test_summary_filters(store):
    finished(store, "old", RunStatus.PASS, started_at="2026-01-01T00:00:00.000000+00:00")
    finished(store, "new", RunStatus.FAIL, started_at="2026-02-01T00:00:00.000000+00:00")
    finished(store, "seq2", RunStatus.PASS, name="other", config_hash="ffff")

    def total(flt):
        return sum(sum(g.status_counts.values()) for g in store.summarize(flt))

    assert total(SummaryFilter()) == 3
    assert total(SummaryFilter(since="2026-01-15T00:00:00.000000+00:00")) == 1
    assert total(SummaryFilter(until="2026-01-15T00:00:00.000000+00:00")) == 2
    assert total(SummaryFilter(sequence_name="other")) == 1
    assert total(SummaryFilter(config_hash="FF")) == 1
    assert store.summarize(SummaryFilter(sequence_name="none")) == []
