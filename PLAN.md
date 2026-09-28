# Mini Test Sequencer — Implementation Plan

## 1. Objective

Build a public Python repository that demonstrates an end-to-end automated test workflow:

1. Start a simulated device that accepts commands over a TCP socket.
2. Run an ordered sequence of tests against a device serial number.
3. Evaluate measurements using limits loaded from YAML.
4. Store run metadata and step results in SQLite.
5. Print a run report and an aggregate yield/failure Pareto summary.

The finished repository should be understandable, reproducible, and easy to demonstrate on a laptop without physical hardware or external services.

## 2. Scope and Decisions

| Area | Initial choice | Rationale |
| --- | --- | --- |
| Language | Python with type hints | Familiar, readable test automation code |
| Device transport | TCP, newline-delimited JSON | Small protocol with explicit message boundaries |
| Configuration | YAML with strict validation | Human-readable steps and limits |
| Database | SQLite through the standard library | Meets the storage requirement without server setup |
| CLI | Standard-library `argparse` | Keeps the dependency list small |
| Dependencies | PyYAML; pytest for development | Add dependencies only for demonstrated needs |
| Execution | Sequential, one device per run | Clear behavior and straightforward troubleshooting |
| License | MIT proposed | Simple permissive license for a public example |

PostgreSQL, parallel device execution, a web dashboard, hardware drivers, and production deployment are outside the initial scope. Keep persistence behind a small interface so PostgreSQL can be added later without changing sequence logic.

## 3. User Workflow

Illustrative CLI commands, to be implemented:

```bash
# Install the project and development dependencies.
python -m pip install -e '.[dev]'

# Terminal 1: start the simulated device.
mini-seq simulator --host 127.0.0.1 --port 9000 --seed 42

# Terminal 2: run one unit using a YAML sequence.
mini-seq run --serial DUT-0001 --config examples/sequence.yaml --db results.sqlite

# Produce a repeatable demonstration batch.
mini-seq demo --units 100 --seed 42 --db demo.sqlite

# Summarize stored results, optionally within a time range.
mini-seq summary --db demo.sqlite
```

The demo command will start an isolated simulator on an available local port, run a controlled mix of unit profiles, and stop the simulator afterward. A seed makes measurements and expected aggregate counts repeatable; identifiers and timestamps remain unique.

## 4. Architecture

```text
CLI → config validation → sequencer → device client → TCP simulator
                              │
                              ├→ limit evaluator
                              └→ SQLite result store → summary queries → console report
```

Keep responsibilities separate:

- **Configuration:** parse YAML, validate fields, and produce typed configuration objects.
- **Device client:** connect, frame requests, validate replies, and enforce deadlines.
- **Simulator:** maintain simple device state and return deterministic measurements.
- **Sequencer:** execute steps, apply limits, assign outcomes, and persist progress.
- **Storage:** initialize the schema, save runs/results, and query aggregates.
- **Reporting:** format per-run results and database summaries.

Suggested repository layout:

```text
mini-test-sequencer/
├── pyproject.toml
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── PLAN.md
├── .gitignore
├── .github/workflows/ci.yml
├── examples/sequence.yaml
├── docs/protocol.md
├── docs/design.md
├── src/mini_sequencer/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── protocol.py
│   ├── client.py
│   ├── simulator.py
│   ├── sequencer.py
│   ├── storage.py
│   └── reporting.py
└── tests/
    ├── unit/
    └── integration/
```

## 5. Socket Protocol and Simulator

Use UTF-8 JSON messages terminated by a newline, with a documented maximum message size. Each request includes a protocol version, request ID, command, and arguments. Each response echoes the request ID and contains either a result or a structured error.

Initial commands:

- `identify`: return model, firmware, and simulator identity.
- `reset`: restore the device to its initial state.
- `measure`: return a numeric measurement and unit for a supported channel.

Simulate supply voltage, idle current, and temperature. Support nominal units, undervoltage, excessive current, timeout, and malformed-response scenarios. Fault scenarios are selected explicitly by the simulator/demo controls rather than being accepted as arbitrary code or shell commands.

Handle partial reads, multiple messages in one receive, disconnects, malformed JSON, unexpected request IDs, unknown commands, and oversized responses. Bind to loopback by default. Do not automatically retry measurements in the initial version; retries could hide unstable behavior or duplicate actions.

## 6. YAML Sequence and Limit Evaluation

Example configuration:

```yaml
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
```

Load YAML safely and validate before connecting to the device. Reject duplicate keys, unknown fields, duplicate step IDs, missing limits, reversed ranges, non-finite values, invalid ports, and nonpositive timeouts. Boolean values must not be accepted as numbers.

Limits are inclusive: a measurement passes when it meets every configured bound. Compare the original numeric value; round only for display. Unit mismatches, missing values, and invalid numeric responses are errors rather than measurement failures.

## 7. Execution and Outcome Rules

Record a run as `RUNNING` before interacting with the device. Identify and reset the device, then execute the configured steps in order.

Step statuses:

- `PASS`: a valid measurement satisfies its limits.
- `FAIL`: a valid measurement violates a limit.
- `ERROR`: communication, protocol, or execution failed.
- `SKIP`: the step was not attempted after an earlier stop condition.

A limit failure continues by default; `stop_on_fail` skips the remaining steps. A transport or protocol error stops the sequence because device state is uncertain. Run status precedence is `ERROR`, then `FAIL`, then `PASS`. A user interruption records `ABORTED` where possible.

Persist each completed step and finalize the run separately. A hard process termination may leave a `RUNNING` record; report it as unfinished rather than counting it as a completed result. Provide a documented recovery command to mark a selected stale run `ABORTED` explicitly.

CLI exit codes: `0` for a passing run, `1` for a limit failure, `2` for configuration or execution errors, and `130` for user interruption.

## 8. Persistence and Traceability

Create a versioned SQLite schema with foreign keys enabled and parameterized queries.

**Runs** store a UUID, serial number, sequence name, UTC start/end timestamps, monotonic duration, final status, device identity, package version, configuration snapshot, configuration hash, and any run-level error.

**Step results** store the run ID, step position/ID, timestamps, duration, measurement, unit, applied bounds, status, and structured error details. Enforce one result per step position per run.

Add indexes supporting time-range, sequence/configuration, serial-number, and failure queries. Commit incremental results so earlier measurements survive a later failure. Keep generated databases out of version control.

## 9. Yield and Pareto Definitions

Make denominators explicit in both documentation and console output:

- **Completed runs:** `PASS + FAIL + ERROR` runs. Retests count as additional runs.
- **Run yield:** passing runs divided by completed runs, multiplied by 100.
- **Other outcomes:** list `ABORTED` and unfinished `RUNNING` counts separately.
- **Failure Pareto:** count `FAIL` step results by step ID, sort descending, and display occurrence count, percentage of failure occurrences, and cumulative percentage.
- **Execution errors:** summarize separately by error category so socket faults are not presented as out-of-limit measurements.

A run can contribute multiple failure occurrences. Stop-on-fail limits which failures are observed; document that effect. Do not label run yield as first-pass yield. Group summaries by sequence and configuration hash by default to avoid silently combining different limits. Show `N/A` for yield with no completed runs and an explicit empty state when no failures exist.

## 10. Implementation Milestones

### Milestone 1 — Project foundation

- Add package metadata, CLI entry point, license, ignore rules, and initial documentation.
- Define typed domain models and configuration validation.
- Deliver a valid example sequence and actionable validation errors.

**Acceptance:** a clean installation exposes CLI help and validates the example configuration.

### Milestone 2 — Socket simulator and client

- Implement framed requests/responses and the simulator command set.
- Add repeatable nominal/fault profiles and client deadlines.
- Document the protocol with request/response examples.

**Acceptance:** the client obtains measurements over a real local socket and handles injected faults predictably.

### Milestone 3 — Sequencing and storage

- Implement inclusive limit evaluation, ordered execution, stopping rules, and outcomes.
- Save run metadata, configuration snapshots, and incremental step results.
- Add per-run console output and documented exit codes.

**Acceptance:** passing, failing, and errored runs produce correct persisted results and exit codes.

### Milestone 4 — Analytics and demo

- Implement grouped yield, failure Pareto, and error summaries.
- Add time/configuration filters and the seeded batch demonstration.
- Include captured sample output in the README.

**Acceptance:** a deterministic batch produces summary counts that agree with independently calculated fixture expectations.

### Milestone 5 — Public repository readiness

- Complete meaningful tests, automated checks, contribution guidance, and troubleshooting.
- Verify installation and the README quickstart in a fresh environment.
- Review tracked files for credentials, local data, databases, and machine-specific paths.
- Prepare the GitHub repository description, initial commit, and public release instructions.
- Confirm the intended GitHub owner/repository name before creating the public remote and pushing.

**Acceptance:** the public repository contains working code, reproducible instructions, license, sample configuration, and passing CI.

## 11. Verification Strategy

- **Unit tests:** inclusive boundaries, one-sided limits, configuration rejection, status precedence, and aggregation denominators.
- **Protocol tests:** fragmented/coalesced messages, mismatched IDs, malformed JSON, unit mismatch, oversized frames, disconnects, and bounded timeouts.
- **Integration tests:** a real simulator on an ephemeral port, a temporary SQLite database, end-to-end pass/fail/error runs, and retained progress after a later step fails.
- **CLI tests:** exit codes, readable configuration errors, empty summaries, repeated serial numbers, and configuration grouping.
- **Demo checks:** expected batch counts and Pareto ordering from fixed fixtures.
- **CI:** install the package, run tests and lint checks on the declared supported Python versions, and verify the package builds. Select exact versions during implementation.

Use temporary databases and isolated socket ports. Avoid timing-sensitive assertions and dependencies on external services.

## 12. Completion Checklist

- [ ] One documented setup path works from a fresh checkout.
- [ ] The simulator communicates through a real TCP socket.
- [ ] YAML limits determine recorded step outcomes.
- [ ] SQLite retains auditable runs and measurements.
- [ ] Yield and Pareto output use documented denominators.
- [ ] Normal operation and representative faults are tested.
- [ ] A seeded demo demonstrates both passing and failing units.
- [ ] CI passes and package installation succeeds.
- [ ] README, protocol documentation, and license are complete.
- [ ] The repository is published publicly under the agreed GitHub account.
