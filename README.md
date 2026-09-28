# mini-test-sequencer

A small, end-to-end automated test workflow you can run on a laptop with no
hardware:

1. A **simulated device** accepts commands over a real TCP socket.
2. A **sequencer** runs an ordered list of tests against one serial number.
3. Measurements are judged against **limits loaded from YAML**.
4. Run metadata and every step result are stored in **SQLite**.
5. Reports show each run, plus an aggregate **yield and failure Pareto**.

```text
CLI → config validation → sequencer → device client → TCP simulator
                              │
                              ├→ limit evaluator
                              └→ SQLite result store → summary queries → console report
```

## Quickstart

Requires Python 3.10 or newer.

```bash
git clone https://github.com/malamoney/mini-test-sequencer.git
cd mini-test-sequencer
python -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

mini-seq validate examples/sequence.yaml
```

Test one unit against the simulator:

```bash
# Terminal 1: start the simulated device (Ctrl+C to stop).
mini-seq simulator --host 127.0.0.1 --port 9000 --seed 42

# Terminal 2: run the example sequence against one unit.
mini-seq run --serial DUT-0001 --config examples/sequence.yaml --db results.sqlite
```

```text
Run      453377a2-81b1-4bc9-9e4b-9e35e8ef0881
Serial   DUT-0001
Sequence basic_device_check (config 5da0bddcf5b0)
Device   MTS-SIM-1 firmware 1.0.0, profile nominal
Started  2026-09-28T04:21:10.273907+00:00  (0.003 s)

#  Step            Measured    Limits          Status
1  supply_voltage  5.0279 V    4.75 .. 5.25 V  PASS
2  idle_current    61.0004 mA  <= 120 mA       PASS
3  temperature     25.5754 C   15 .. 45 C      PASS

Result   PASS
```

Restart the simulator with `--profile undervoltage` (or `overcurrent`,
`overtemperature`, `timeout`, `malformed`) to see failures and errors.

Run a repeatable batch of 100 units and summarize it. The demo starts its own
simulator on a free loopback port and stops it afterwards:

```bash
mini-seq demo --units 100 --seed 42 --db demo.sqlite
```

```text
Running 100 units (seed 42); non-passing units:
  DEMO-42-0003  profile overcurrent     FAIL
  DEMO-42-0018  profile overcurrent     FAIL
  DEMO-42-0024  profile timeout         ERROR
  ...
  DEMO-42-0097  profile overtemperature FAIL

Summary of demo.sqlite (since 2026-09-28T04:21:07.795825+00:00, config 8ff06449e874)

Sequence basic_device_check  config 8ff06449e874
  Runs started:   2026-09-28T04:21:07.798547+00:00 .. 2026-09-28T04:21:09.024618+00:00
  Completed runs: 100  (PASS 82, FAIL 14, ERROR 4)
  Run yield:      82.0%  = PASS runs / completed runs
  Not counted:    ABORTED 0, unfinished RUNNING 0

  Failure Pareto (FAIL step results in completed runs; one run may add several):
    Step            Count  Share  Cumulative
    supply_voltage      6  42.9%       42.9%
    idle_current        5  35.7%       78.6%
    temperature         3  21.4%      100.0%

  Execution errors (one per ERROR run, not limit failures):
    Category  Runs
    protocol     2
    timeout      2
```

The seed fixes which units get which simulator profile and every measured
value, so the counts above are the same on every machine. Run IDs and
timestamps are always unique. The demo uses a 0.5 s device timeout (`--timeout`)
so the timeout faults finish quickly; that is why its configuration hash differs
from the example file's.

Summarize any database, optionally filtered:

```bash
mini-seq summary --db demo.sqlite
mini-seq summary --db demo.sqlite --since 2026-09-01 --until 2026-10-01T00:00:00Z
mini-seq summary --db demo.sqlite --sequence basic_device_check --config-hash 8ff0 --json
```

## Commands

| Command | Purpose |
| --- | --- |
| `mini-seq validate CONFIG` | Check a sequence file without touching a device |
| `mini-seq simulator [--host] [--port] [--seed] [--profile]` | Run the simulated device |
| `mini-seq run --serial SN --config CONFIG --db DB [--host] [--port] [--timeout]` | Test one unit |
| `mini-seq demo --db DB [--units N] [--seed S] [--timeout T] [--config CONFIG]` | Seeded batch run |
| `mini-seq summary --db DB [--since] [--until] [--sequence] [--config-hash] [--json]` | Yield, Pareto, errors |
| `mini-seq report RUN_ID --db DB` | Show one stored run (ID prefix allowed) |
| `mini-seq runs --db DB [--status S] [--limit N]` | List stored runs, newest first |
| `mini-seq abort RUN_ID --db DB [--reason TEXT]` | Mark a stale `RUNNING` run `ABORTED` |

`python -m mini_sequencer` works in place of `mini-seq`.

### Exit codes (`run`)

| Code | Meaning |
| --- | --- |
| `0` | Every step passed |
| `1` | At least one limit failure, no errors |
| `2` | Configuration error, or a communication/protocol/execution error |
| `130` | Interrupted by the user (the run is recorded `ABORTED`) |

Other commands return `0` on success and `2` on error.

## Sequence files

See [`examples/sequence.yaml`](examples/sequence.yaml). Every file is validated
before any connection is made. Validation rejects duplicate keys, unknown
fields, duplicate step IDs, missing limits, `min` greater than `max`, `NaN` or
infinite values, booleans used as numbers, invalid ports, and non-positive
timeouts. All problems are reported together, each with its location:

```text
error: invalid configuration bad.yaml:
  - device.port: must be an integer from 1 to 65535, got 0
  - steps[0].limits: min (5.5) is greater than max (5.25)
```

**Limits are inclusive.** A measurement passes when it meets every configured
bound (`min`, `max`, or both). The unrounded value is compared; rounding is only
for display. A reported unit that differs from the step's `unit`, a missing
value, or a non-numeric value is an `ERROR`, not a `FAIL`.

## Outcomes

| Step status | Meaning |
| --- | --- |
| `PASS` | A valid measurement met its limits |
| `FAIL` | A valid measurement violated a limit |
| `ERROR` | Communication, protocol, or execution failed |
| `SKIP` | Not attempted because an earlier step stopped the sequence |

- A limit failure continues to the next step unless `execution.stop_on_fail: true`.
- Any error stops the sequence, because the device's state is uncertain.
  Measurements are never retried automatically.
- Run status: `ERROR` if any step errored (or the device could not be reached),
  else `FAIL` if any step failed, else `PASS`. Ctrl+C records `ABORTED`.

## How the summary counts

- **Completed runs** = `PASS` + `FAIL` + `ERROR` runs. Retesting a serial number
  adds another run; runs are never merged per unit.
- **Run yield** = `PASS` runs ÷ completed runs × 100. It is *not* first-pass
  yield. With no completed runs it shows `N/A`.
- **Not counted**: `ABORTED` runs and unfinished `RUNNING` runs are listed
  separately and excluded from yield.
- **Failure Pareto** counts `FAIL` step results by step ID, in completed runs,
  sorted by count (ties by step ID), with each step's share and the cumulative
  share. One run can contribute several failures. With `stop_on_fail`, failures
  after the first are never observed, so the Pareto under-counts later steps.
- **Execution errors** are counted once per `ERROR` run by category
  (`connection`, `timeout`, `protocol`, `device`, `unit_mismatch`, `internal`),
  so socket faults are never mixed with out-of-limit measurements.
- Results are **grouped by sequence name and configuration hash**, so runs made
  with different limits are never silently combined. The hash covers the steps,
  limits, stop rule, and timeout, but not the device host or port.

## Storage

SQLite, schema version 1 (`PRAGMA user_version`), with foreign keys on and only
parameterized queries. A run is inserted as `RUNNING` before the device is
contacted, each step result is committed as soon as it completes, and the run is
finalized separately. Every run stores the serial number, UTC timestamps,
monotonic duration, device identity, package version, the full effective
configuration, and its hash. See [`docs/design.md`](docs/design.md).

### Recovering an unfinished run

If the process is killed, its run stays `RUNNING`. Summaries list it as
unfinished and leave it out of yield. To close it explicitly:

```bash
mini-seq runs --db results.sqlite --status RUNNING
mini-seq abort 1b2c3d4e --db results.sqlite --reason "bench power loss"
```

Only `RUNNING` runs can be aborted; finished results are never changed.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `cannot connect to 127.0.0.1:9000: Connection refused` (exit 2) | The simulator is not running, or is on another port. Start it, or pass `--port`. |
| `cannot listen on 127.0.0.1:9000: Address already in use` | Another simulator is running. Stop it or use `--port 0` for any free port. |
| `no reply to 'measure' within 2 s` | The device is stalled (for example `--profile timeout`). Raise `device.timeout_s` only if the device is really slow. |
| `result database ... does not exist` | `summary`, `report`, `runs`, and `abort` never create a database. Check the `--db` path. |
| `... is not a mini-sequencer result database` | `--db` points at some other SQLite file. |
| Summary shows two groups for one sequence | The limits, stop rule, or timeout changed between runs, so the configuration hash differs. This is intentional. |

## Development

```bash
python -m pip install -e '.[dev]'
pytest               # unit + integration tests (real sockets, temporary databases)
ruff check .         # lint
ruff format --check .
python -m build      # sdist + wheel
```

See [CONTRIBUTING.md](CONTRIBUTING.md), the wire protocol in
[docs/protocol.md](docs/protocol.md), and the original plan in [PLAN.md](PLAN.md).

## Scope

Out of scope for this version: PostgreSQL, testing several units in parallel, a
web dashboard, real hardware drivers, and production deployment. Storage sits
behind a small `ResultStore` interface so another database can be added without
touching the sequencer.

## License

[MIT](LICENSE)
