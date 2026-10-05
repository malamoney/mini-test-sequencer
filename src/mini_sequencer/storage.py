"""Result persistence.

:class:`ResultStore` is the interface the sequencer and reports depend on;
:class:`SQLiteResultStore` implements it with the standard library. Another
backend (for example PostgreSQL) can implement the same methods without
changing sequencing logic.

Every write commits immediately, so earlier step results survive a later
failure or a crash.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mini_sequencer.models import RunRecord, RunStatus, StepRecord, StepStatus

SCHEMA_VERSION = 1

_RUN_STATUSES = ", ".join(f"'{s.value}'" for s in RunStatus)
_STEP_STATUSES = ", ".join(f"'{s.value}'" for s in StepStatus)

SCHEMA = f"""
CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    serial          TEXT NOT NULL,
    sequence_name   TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    config_snapshot TEXT NOT NULL,
    package_version TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    duration_s      REAL,
    status          TEXT NOT NULL CHECK (status IN ({_RUN_STATUSES})),
    device_identity TEXT,
    error_category  TEXT,
    error_message   TEXT
);

CREATE TABLE step_results (
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL CHECK (position >= 1),
    step_id         TEXT NOT NULL,
    command         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    unit            TEXT NOT NULL,
    limit_min       REAL,
    limit_max       REAL,
    measurement     REAL,
    measured_unit   TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    duration_s      REAL,
    status          TEXT NOT NULL CHECK (status IN ({_STEP_STATUSES})),
    error_category  TEXT,
    error_message   TEXT,
    error_detail    TEXT,
    PRIMARY KEY (run_id, position),
    UNIQUE (run_id, step_id)
);

CREATE INDEX idx_runs_started_at ON runs (started_at);
CREATE INDEX idx_runs_sequence_config ON runs (sequence_name, config_hash);
CREATE INDEX idx_runs_serial ON runs (serial);
CREATE INDEX idx_runs_status ON runs (status);
CREATE INDEX idx_steps_status_step ON step_results (status, step_id);
"""


class StorageError(Exception):
    """The result database is unusable or an operation is not allowed."""


@dataclass(frozen=True)
class SummaryFilter:
    since: str | None = None  # inclusive, ISO 8601 UTC
    until: str | None = None  # exclusive, ISO 8601 UTC
    sequence_name: str | None = None
    config_hash: str | None = None  # full hash or unique prefix


@dataclass
class GroupSummary:
    """Aggregates for one (sequence name, configuration hash) group."""

    sequence_name: str
    config_hash: str
    status_counts: dict[RunStatus, int]
    failures_by_step: list[tuple[str, int]] = field(default_factory=list)
    errors_by_category: list[tuple[str, int]] = field(default_factory=list)
    first_started_at: str | None = None
    last_started_at: str | None = None


class ResultStore(Protocol):
    def create_run(self, run: RunRecord) -> None: ...
    def set_device_identity(self, run_id: str, identity: dict[str, Any]) -> None: ...
    def save_step(self, run_id: str, step: StepRecord) -> None: ...
    def finalize_run(
        self,
        run_id: str,
        status: RunStatus,
        ended_at: str,
        duration_s: float,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> None: ...
    def get_run(self, run_id: str) -> RunRecord | None: ...
    def list_runs(self, status: RunStatus | None = None, limit: int = 50) -> list[RunRecord]: ...
    def get_runs(self, flt: SummaryFilter) -> list[RunRecord]: ...
    def abort_stale_run(self, run_id: str, ended_at: str, reason: str) -> RunRecord: ...
    def summarize(self, flt: SummaryFilter) -> list[GroupSummary]: ...
    def close(self) -> None: ...


class SQLiteResultStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        try:
            self._conn = sqlite3.connect(self.path, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._init_schema()
        except sqlite3.DatabaseError as exc:
            raise StorageError(f"cannot open result database {self.path}: {exc}") from exc

    def __enter__(self) -> SQLiteResultStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise StorageError(
                f"{self.path} has schema version {version}; this build supports {SCHEMA_VERSION}"
            )
        tables = self._conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        if tables:
            raise StorageError(f"{self.path} is not a mini-sequencer result database")
        with self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- writes -----------------------------------------------------------

    def create_run(self, run: RunRecord) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO runs (id, serial, sequence_name, config_hash, config_snapshot,
                       package_version, started_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.id,
                    run.serial,
                    run.sequence_name,
                    run.config_hash,
                    run.config_snapshot,
                    run.package_version,
                    run.started_at,
                    run.status.value,
                ),
            )

    def set_device_identity(self, run_id: str, identity: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET device_identity = ? WHERE id = ?",
                (json.dumps(identity, sort_keys=True), run_id),
            )

    def save_step(self, run_id: str, step: StepRecord) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO step_results (run_id, position, step_id, command, channel, unit,
                       limit_min, limit_max, measurement, measured_unit, started_at, ended_at,
                       duration_s, status, error_category, error_message, error_detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    step.position,
                    step.step_id,
                    step.command,
                    step.channel,
                    step.unit,
                    step.limit_min,
                    step.limit_max,
                    step.measurement,
                    step.measured_unit,
                    step.started_at,
                    step.ended_at,
                    step.duration_s,
                    step.status.value,
                    step.error_category,
                    step.error_message,
                    json.dumps(step.error_detail, sort_keys=True) if step.error_detail else None,
                ),
            )

    def finalize_run(
        self,
        run_id: str,
        status: RunStatus,
        ended_at: str,
        duration_s: float,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if status is RunStatus.RUNNING:
            raise ValueError("a run cannot be finalized as RUNNING")
        with self._conn:
            cursor = self._conn.execute(
                """UPDATE runs SET status = ?, ended_at = ?, duration_s = ?,
                       error_category = ?, error_message = ?
                   WHERE id = ? AND status = 'RUNNING'""",
                (status.value, ended_at, duration_s, error_category, error_message, run_id),
            )
        if cursor.rowcount != 1:
            raise StorageError(f"run {run_id} is not RUNNING and cannot be finalized")

    def abort_stale_run(self, run_id: str, ended_at: str, reason: str) -> RunRecord:
        """Mark a run left ``RUNNING`` (for example by a killed process) as ``ABORTED``."""
        matches = self._conn.execute(
            "SELECT id, status FROM runs WHERE substr(id, 1, length(?)) = ?", (run_id, run_id)
        ).fetchall()
        if not matches:
            raise StorageError(f"no run matches {run_id!r}")
        if len(matches) > 1:
            raise StorageError(f"{run_id!r} matches {len(matches)} runs; use more characters")
        full_id, status = matches[0]["id"], matches[0]["status"]
        if status != RunStatus.RUNNING.value:
            raise StorageError(f"run {full_id} is {status}, not RUNNING; nothing to abort")
        with self._conn:
            self._conn.execute(
                """UPDATE runs SET status = 'ABORTED', ended_at = ?, error_category = 'aborted',
                       error_message = ? WHERE id = ? AND status = 'RUNNING'""",
                (ended_at, reason, full_id),
            )
        run = self.get_run(full_id)
        assert run is not None
        return run

    # -- reads ------------------------------------------------------------

    def get_run(self, run_id: str) -> RunRecord | None:
        """Fetch a run by full ID or unique prefix."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE substr(id, 1, length(?)) = ? LIMIT 2", (run_id, run_id)
        ).fetchall()
        if len(rows) != 1:
            if len(rows) > 1:
                raise StorageError(f"{run_id!r} matches more than one run; use more characters")
            return None
        run = _run_from_row(rows[0])
        steps = self._conn.execute(
            "SELECT * FROM step_results WHERE run_id = ? ORDER BY position", (run.id,)
        ).fetchall()
        run.steps = [_step_from_row(row) for row in steps]
        return run

    def list_runs(self, status: RunStatus | None = None, limit: int = 50) -> list[RunRecord]:
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY started_at DESC, id LIMIT ?"
        params.append(limit)
        return [_run_from_row(row) for row in self._conn.execute(query, params)]

    def get_runs(self, flt: SummaryFilter) -> list[RunRecord]:
        """Runs matching ``flt`` with their steps, oldest first."""
        where, params = _run_filter(flt)
        runs = {
            row["id"]: _run_from_row(row)
            for row in self._conn.execute(
                f"SELECT * FROM runs r WHERE {where} ORDER BY r.started_at, r.id", params
            )
        }
        for row in self._conn.execute(
            f"""SELECT s.* FROM step_results s JOIN runs r ON r.id = s.run_id
                WHERE {where} ORDER BY s.run_id, s.position""",
            params,
        ):
            runs[row["run_id"]].steps.append(_step_from_row(row))
        return list(runs.values())

    def summarize(self, flt: SummaryFilter) -> list[GroupSummary]:
        where, params = _run_filter(flt)
        groups: dict[tuple[str, str], GroupSummary] = {}

        for row in self._conn.execute(
            f"""SELECT sequence_name, config_hash, status, count(*) AS n,
                       min(started_at) AS first, max(started_at) AS last
                FROM runs r WHERE {where}
                GROUP BY sequence_name, config_hash, status""",
            params,
        ):
            group = groups.setdefault(
                (row["sequence_name"], row["config_hash"]),
                GroupSummary(
                    sequence_name=row["sequence_name"],
                    config_hash=row["config_hash"],
                    status_counts={s: 0 for s in RunStatus},
                ),
            )
            group.status_counts[RunStatus(row["status"])] = row["n"]
            if group.first_started_at is None or row["first"] < group.first_started_at:
                group.first_started_at = row["first"]
            if group.last_started_at is None or row["last"] > group.last_started_at:
                group.last_started_at = row["last"]

        # Failure occurrences from completed runs only; unfinished runs are not results.
        for row in self._conn.execute(
            f"""SELECT r.sequence_name, r.config_hash, s.step_id, count(*) AS n
                FROM step_results s JOIN runs r ON r.id = s.run_id
                WHERE s.status = 'FAIL' AND r.status IN ('PASS', 'FAIL', 'ERROR') AND {where}
                GROUP BY r.sequence_name, r.config_hash, s.step_id
                ORDER BY n DESC, s.step_id""",
            params,
        ):
            groups[(row["sequence_name"], row["config_hash"])].failures_by_step.append(
                (row["step_id"], row["n"])
            )

        for row in self._conn.execute(
            f"""SELECT sequence_name, config_hash,
                       coalesce(error_category, 'unknown') AS category, count(*) AS n
                FROM runs r WHERE r.status = 'ERROR' AND {where}
                GROUP BY sequence_name, config_hash, category
                ORDER BY n DESC, category""",
            params,
        ):
            groups[(row["sequence_name"], row["config_hash"])].errors_by_category.append(
                (row["category"], row["n"])
            )

        return sorted(groups.values(), key=lambda g: (g.sequence_name, g.first_started_at or ""))


def _run_filter(flt: SummaryFilter) -> tuple[str, list[Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    if flt.since is not None:
        clauses.append("r.started_at >= ?")
        params.append(flt.since)
    if flt.until is not None:
        clauses.append("r.started_at < ?")
        params.append(flt.until)
    if flt.sequence_name is not None:
        clauses.append("r.sequence_name = ?")
        params.append(flt.sequence_name)
    if flt.config_hash is not None:
        clauses.append("substr(r.config_hash, 1, length(?)) = ?")
        params.extend([flt.config_hash.lower()] * 2)
    return " AND ".join(clauses), params


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        serial=row["serial"],
        sequence_name=row["sequence_name"],
        config_hash=row["config_hash"],
        config_snapshot=row["config_snapshot"],
        package_version=row["package_version"],
        started_at=row["started_at"],
        status=RunStatus(row["status"]),
        ended_at=row["ended_at"],
        duration_s=row["duration_s"],
        device_identity=json.loads(row["device_identity"]) if row["device_identity"] else None,
        error_category=row["error_category"],
        error_message=row["error_message"],
    )


def _step_from_row(row: sqlite3.Row) -> StepRecord:
    return StepRecord(
        position=row["position"],
        step_id=row["step_id"],
        command=row["command"],
        channel=row["channel"],
        status=StepStatus(row["status"]),
        unit=row["unit"],
        limit_min=row["limit_min"],
        limit_max=row["limit_max"],
        measurement=row["measurement"],
        measured_unit=row["measured_unit"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_s=row["duration_s"],
        error_category=row["error_category"],
        error_message=row["error_message"],
        error_detail=json.loads(row["error_detail"]) if row["error_detail"] else None,
    )
