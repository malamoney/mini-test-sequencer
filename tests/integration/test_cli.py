from __future__ import annotations

import json
import subprocess
import sys

import pytest

from mini_sequencer.cli import EXIT_ERROR, EXIT_FAIL, EXIT_PASS, main
from mini_sequencer.models import RunRecord, RunStatus
from mini_sequencer.storage import SQLiteResultStore
from tests.conftest import EXAMPLE_CONFIG


def run_cli(simulator, db, serial="SN-1", *extra):
    host, port = simulator.address
    return main(
        [
            "run",
            "--serial",
            serial,
            "--config",
            str(EXAMPLE_CONFIG),
            "--db",
            str(db),
            "--host",
            host,
            "--port",
            str(port),
            "--timeout",
            "0.3",
            *extra,
        ]
    )


def test_help_via_module_entry_point():
    out = subprocess.run(
        [sys.executable, "-m", "mini_sequencer", "--help"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert "simulator" in out.stdout and "summary" in out.stdout


def test_validate_example(capsys):
    assert main(["validate", str(EXAMPLE_CONFIG)]) == EXIT_PASS
    assert "OK" in capsys.readouterr().out


def test_validate_reports_readable_errors(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(EXAMPLE_CONFIG.read_text().replace("min: 4.75", "min: 5.5"))
    assert main(["validate", str(bad)]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "bad.yaml" in err
    assert "steps[0].limits: min (5.5) is greater than max (5.25)" in err


def test_run_invalid_config_does_not_touch_device_or_db(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\n")
    db = tmp_path / "r.sqlite"
    rc = main(["run", "--serial", "X", "--config", str(bad), "--db", str(db)])
    assert rc == EXIT_ERROR
    assert not db.exists()


def test_exit_codes_pass_fail_error(simulator, tmp_path, capsys):
    db = tmp_path / "r.sqlite"
    assert run_cli(simulator, db, "SN-P") == EXIT_PASS
    simulator.configure("overcurrent", 1)
    assert run_cli(simulator, db, "SN-F") == EXIT_FAIL
    simulator.configure("malformed", 1)
    assert run_cli(simulator, db, "SN-E") == EXIT_ERROR
    out = capsys.readouterr().out
    assert "Result   PASS" in out and "Result   FAIL" in out and "Result   ERROR" in out
    assert "[protocol]" in out


def test_unreachable_device_exit_code(tmp_path, simulator):
    host, port = simulator.address
    simulator.stop()
    assert run_cli(simulator, tmp_path / "r.sqlite") == EXIT_ERROR


def test_bad_serial_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as info:
        main(["run", "--serial", "  ", "--config", str(EXAMPLE_CONFIG), "--db", "x"])
    assert info.value.code == 2


def test_summary_empty_database(tmp_path, capsys):
    db = tmp_path / "empty.sqlite"
    SQLiteResultStore(db).close()
    assert main(["summary", "--db", str(db)]) == EXIT_PASS
    assert "No runs match." in capsys.readouterr().out


def test_summary_missing_database_is_error(tmp_path, capsys):
    assert main(["summary", "--db", str(tmp_path / "nope.sqlite")]) == EXIT_ERROR
    assert "does not exist" in capsys.readouterr().err
    assert not (tmp_path / "nope.sqlite").exists()


def test_summary_groups_by_configuration(simulator, tmp_path, capsys):
    db = tmp_path / "r.sqlite"
    run_cli(simulator, db, "SN-1")
    run_cli(simulator, db, "SN-1")  # retest: an additional run
    other = tmp_path / "other.yaml"
    other.write_text(EXAMPLE_CONFIG.read_text().replace("max: 120.0", "max: 50.0"))
    host, port = simulator.address
    main(
        [
            "run",
            "--serial",
            "SN-2",
            "--config",
            str(other),
            "--db",
            str(db),
            "--host",
            host,
            "--port",
            str(port),
        ]
    )
    capsys.readouterr()

    assert main(["summary", "--db", str(db), "--json"]) == EXIT_PASS
    groups = json.loads(capsys.readouterr().out)
    assert len(groups) == 2
    by_completed = sorted(groups, key=lambda g: g["completed_runs"])
    assert by_completed[0]["status_counts"]["FAIL"] == 1
    assert by_completed[0]["failure_pareto"][0]["step_id"] == "idle_current"
    assert by_completed[1]["completed_runs"] == 2
    assert by_completed[1]["run_yield_percent"] == 100.0


def test_summary_time_filter(simulator, tmp_path, capsys):
    db = tmp_path / "r.sqlite"
    run_cli(simulator, db)
    capsys.readouterr()
    assert main(["summary", "--db", str(db), "--until", "2000-01-01"]) == EXIT_PASS
    assert "No runs match." in capsys.readouterr().out
    assert main(["summary", "--db", str(db), "--since", "2000-01-01T00:00:00Z"]) == EXIT_PASS
    assert "Completed runs: 1" in capsys.readouterr().out


def test_summary_rejects_bad_timestamp():
    with pytest.raises(SystemExit):
        main(["summary", "--db", "x", "--since", "yesterday"])


def test_report_runs_and_abort_recovery(tmp_path, capsys):
    db = tmp_path / "r.sqlite"
    with SQLiteResultStore(db) as store:
        store.create_run(
            RunRecord(
                id="11111111-stale",
                serial="SN",
                sequence_name="seq",
                config_hash="h",
                config_snapshot="{}",
                package_version="t",
                started_at="2026-01-01T00:00:00.000000+00:00",
                status=RunStatus.RUNNING,
            )
        )
    assert main(["runs", "--db", str(db), "--status", "RUNNING"]) == EXIT_PASS
    assert "11111111-stale" in capsys.readouterr().out

    assert main(["report", "1111", "--db", str(db)]) == EXIT_PASS
    assert "unfinished" in capsys.readouterr().out

    assert main(["summary", "--db", str(db)]) == EXIT_PASS
    out = capsys.readouterr().out
    assert "unfinished RUNNING 1" in out and "N/A" in out

    assert main(["abort", "1111", "--db", str(db)]) == EXIT_PASS
    assert main(["abort", "1111", "--db", str(db)]) == EXIT_ERROR  # already aborted
    capsys.readouterr()
    assert main(["summary", "--db", str(db)]) == EXIT_PASS
    assert "ABORTED 1, unfinished RUNNING 0" in capsys.readouterr().out


def test_report_unknown_run(tmp_path, capsys):
    db = tmp_path / "r.sqlite"
    SQLiteResultStore(db).close()
    assert main(["report", "nope", "--db", str(db)]) == EXIT_ERROR


def test_interrupt_exit_code(monkeypatch, tmp_path, capsys):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("mini_sequencer.cli.run_sequence", interrupted)
    rc = main(
        [
            "run",
            "--serial",
            "SN",
            "--config",
            str(EXAMPLE_CONFIG),
            "--db",
            str(tmp_path / "r.sqlite"),
        ]
    )
    assert rc == 130
    assert "ABORTED" in capsys.readouterr().err
