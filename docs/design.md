# Design

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Parse YAML safely, reject duplicate keys, validate every field, produce typed objects, compute the snapshot and hash |
| `models.py` | Dataclasses and status enums shared by all layers |
| `protocol.py` | Newline-delimited JSON framing and message helpers |
| `client.py` | Connect, send requests, enforce deadlines, validate replies |
| `simulator.py` | Threaded TCP device with seeded measurements and fault profiles |
| `sequencer.py` | Run steps in order, evaluate limits, assign outcomes, persist progress |
| `storage.py` | `ResultStore` interface and its SQLite implementation, including summary queries |
| `reporting.py` | Yield and Pareto math; text and JSON formatting |
| `html_report.py` | Self-contained HTML results page built from the summary and stored runs (template in `templates/results.html`) |
| `demo.py` | Seeded plan of unit profiles and the batch runner |
| `cli.py` | `argparse` commands and exit codes |

The sequencer depends on two narrow interfaces: a `Device` (identify, reset,
measure) produced by a factory, and a `ResultStore`. Tests substitute both;
a PostgreSQL store would implement `ResultStore` alone.

## Run lifecycle

1. Load and validate the configuration. Nothing touches the device or database
   if this fails (exit 2).
2. Insert the run as `RUNNING` with the configuration snapshot and hash.
3. Connect, `identify` (stored on the run), and `reset`. A failure here is a
   run-level `ERROR`; every step is recorded `SKIP`.
4. For each step: `measure`, check the unit, evaluate limits, and commit the
   result. On `ERROR`, stop. On `FAIL` with `stop_on_fail`, stop. Remaining
   steps are recorded `SKIP`.
5. Finalize the run with its status, end time, monotonic duration, and the
   error category and message if any.

On Ctrl+C the run is finalized `ABORTED`, the step in progress is not recorded,
and the CLI exits 130. If the process is killed outright, the run stays
`RUNNING`; summaries show it as unfinished and `mini-seq abort` closes it.

Unexpected exceptions inside a step become an `ERROR` with category `internal`
so a bug still leaves an auditable record.

## Schema (version 1)

`runs`: `id` (UUID), `serial`, `sequence_name`, `config_hash`,
`config_snapshot` (canonical JSON), `package_version`, `started_at`,
`ended_at` (ISO 8601 UTC), `duration_s` (monotonic), `status`,
`device_identity` (JSON), `error_category`, `error_message`.

`step_results`: primary key (`run_id`, `position`), unique (`run_id`,
`step_id`); `command`, `channel`, `unit`, `limit_min`, `limit_max`,
`measurement`, `measured_unit`, timestamps, `duration_s`, `status`,
`error_category`, `error_message`, `error_detail` (JSON).

Status columns have `CHECK` constraints. Indexes support queries by start time,
sequence and configuration, serial number, run status, and failing step.

Opening a database that has tables but no schema version, or a newer version,
is refused rather than modified.

## Configuration hash

SHA-256 over the canonical JSON of the validated configuration, excluding
`device.host` and `device.port`. Two runs share a hash exactly when they used
the same steps, limits, stop rule, and timeout. Summaries group by
(sequence name, hash) so different limits are never pooled. The stored snapshot
does include host and port, for traceability.

## Determinism

- The simulator draws measurements from `random.Random(seed)`; `reset` rewinds it.
- The demo derives each unit's profile (shuffled, exact proportions by largest
  remainder) and measurement seed from the batch seed.
- Timestamps and run IDs are real and unique; only outcomes and values repeat.

Tests assert the demo's counts against expectations computed from the plan
alone, without reading the database.

## Decisions

- **No retries.** A retry could hide an unstable unit or repeat an action with
  side effects.
- **Every error stops the sequence**, including a unit mismatch. Continuing
  after the device misbehaves would record measurements of unknown validity.
- **Inclusive limits on raw values.** Rounding happens only in the report.
- **Standard library first.** The only runtime dependency is PyYAML.
