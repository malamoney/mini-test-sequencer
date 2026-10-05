"""Console formatting for run reports and database summaries.

Definitions (also in README and ``docs/design.md``):

* Completed runs = PASS + FAIL + ERROR runs. Retests count as additional runs.
* Run yield = PASS runs / completed runs x 100. It is not first-pass yield.
* ABORTED and unfinished RUNNING runs are listed separately and excluded.
* Failure Pareto counts FAIL step results by step ID. One run can contribute
  several occurrences; with ``stop_on_fail`` later failures go unobserved.
* Execution errors are counted once per ERROR run, by error category.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mini_sequencer.models import COMPLETED_RUN_STATUSES, RunRecord, RunStatus, StepRecord
from mini_sequencer.storage import GroupSummary, SummaryFilter

HASH_DISPLAY_CHARS = 12


@dataclass(frozen=True)
class ParetoRow:
    step_id: str
    count: int
    percent: float
    cumulative_percent: float


def run_yield(passed: int, completed: int) -> float | None:
    """Percentage of completed runs that passed, or ``None`` if nothing completed."""
    if completed < 0 or passed < 0 or passed > completed:
        raise ValueError("require 0 <= passed <= completed")
    if completed == 0:
        return None
    return 100.0 * passed / completed


def pareto(counts: list[tuple[str, int]]) -> list[ParetoRow]:
    """Sort by count (descending, ties by step ID) and add share / cumulative share."""
    ordered = sorted(((k, n) for k, n in counts if n > 0), key=lambda kv: (-kv[1], kv[0]))
    total = sum(n for _, n in ordered)
    rows: list[ParetoRow] = []
    running = 0
    for step_id, n in ordered:
        running += n
        rows.append(ParetoRow(step_id, n, 100.0 * n / total, 100.0 * running / total))
    return rows


def format_value(value: float | None, digits: int = 4) -> str:
    """Round for display only; stored and compared values are never rounded."""
    if value is None:
        return "-"
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def format_limits(step: StepRecord) -> str:
    low, high = step.limit_min, step.limit_max
    if low is not None and high is not None:
        text = f"{format_value(low)} .. {format_value(high)}"
    elif low is not None:
        text = f">= {format_value(low)}"
    else:
        text = f"<= {format_value(high)}"
    return f"{text} {step.unit}"


def _table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> list[str]:
    right = right or set()
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)
    ]

    def line(cells: list[str]) -> str:
        parts = [
            c.rjust(w) if i in right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ]
        return "  ".join(parts).rstrip()

    return [line(headers)] + [line(r) for r in rows]


def format_run_report(run: RunRecord) -> str:
    lines = [
        f"Run      {run.id}",
        f"Serial   {run.serial}",
        f"Sequence {run.sequence_name} (config {run.config_hash[:HASH_DISPLAY_CHARS]})",
    ]
    if run.device_identity:
        ident = run.device_identity
        extra = f", profile {ident['profile']}" if "profile" in ident else ""
        lines.append(f"Device   {ident.get('model')} firmware {ident.get('firmware')}{extra}")
    timing = f"Started  {run.started_at}"
    if run.duration_s is not None:
        timing += f"  ({run.duration_s:.3f} s)"
    lines.append(timing)
    lines.append("")

    rows = []
    for step in run.steps:
        if step.measurement is not None:
            measured = f"{format_value(step.measurement)} {step.measured_unit or ''}".rstrip()
        else:
            measured = "-"
        rows.append(
            [str(step.position), step.step_id, measured, format_limits(step), step.status.value]
        )
    lines.extend(_table(["#", "Step", "Measured", "Limits", "Status"], rows, right={0}))
    for step in run.steps:
        if step.error_message:
            lines.append(f"  {step.step_id}: [{step.error_category}] {step.error_message}")
    lines.append("")
    if run.error_category and not any(s.error_message for s in run.steps):
        lines.append(f"Error    [{run.error_category}] {run.error_message}")
    unfinished = " (unfinished: process ended before the run was finalized)"
    lines.append(
        f"Result   {run.status.value}{unfinished if run.status is RunStatus.RUNNING else ''}"
    )
    return "\n".join(lines)


def format_run_list(runs: list[RunRecord]) -> str:
    if not runs:
        return "No runs found."
    rows = [[r.id, r.started_at, r.serial, r.sequence_name, r.status.value] for r in runs]
    return "\n".join(_table(["Run ID", "Started (UTC)", "Serial", "Sequence", "Status"], rows))


def describe_filter(flt: SummaryFilter) -> str:
    parts = []
    if flt.since:
        parts.append(f"since {flt.since}")
    if flt.until:
        parts.append(f"until {flt.until}")
    if flt.sequence_name:
        parts.append(f"sequence {flt.sequence_name}")
    if flt.config_hash:
        parts.append(f"config {flt.config_hash[:HASH_DISPLAY_CHARS]}")
    return ", ".join(parts) if parts else "all runs"


def format_summary(groups: list[GroupSummary], flt: SummaryFilter, source: str) -> str:
    lines = [f"Summary of {source} ({describe_filter(flt)})"]
    if not groups:
        lines += ["", "No runs match."]
        return "\n".join(lines)
    for group in groups:
        lines += [""] + format_group(group)
    return "\n".join(lines)


def format_group(group: GroupSummary) -> list[str]:
    counts = group.status_counts
    completed = sum(counts[s] for s in COMPLETED_RUN_STATUSES)
    passed = counts[RunStatus.PASS]
    yld = run_yield(passed, completed)
    yield_text = "N/A (no completed runs)" if yld is None else f"{yld:.1f}%"

    lines = [
        f"Sequence {group.sequence_name}  config {group.config_hash[:HASH_DISPLAY_CHARS]}",
        f"  Runs started:   {group.first_started_at} .. {group.last_started_at}",
        f"  Completed runs: {completed}  (PASS {passed}, FAIL {counts[RunStatus.FAIL]}, "
        f"ERROR {counts[RunStatus.ERROR]})",
        f"  Run yield:      {yield_text}  = PASS runs / completed runs",
        f"  Not counted:    ABORTED {counts[RunStatus.ABORTED]}, "
        f"unfinished RUNNING {counts[RunStatus.RUNNING]}",
        "",
        "  Failure Pareto (FAIL step results in completed runs; one run may add several):",
    ]
    rows = pareto(group.failures_by_step)
    if rows:
        table = _table(
            ["Step", "Count", "Share", "Cumulative"],
            [
                [r.step_id, str(r.count), f"{r.percent:.1f}%", f"{r.cumulative_percent:.1f}%"]
                for r in rows
            ],
            right={1, 2, 3},
        )
        lines += ["    " + line for line in table]
    else:
        lines.append("    No limit failures recorded.")

    lines += ["", "  Execution errors (one per ERROR run, not limit failures):"]
    if group.errors_by_category:
        table = _table(
            ["Category", "Runs"],
            [[category, str(n)] for category, n in group.errors_by_category],
            right={1},
        )
        lines += ["    " + line for line in table]
    else:
        lines.append("    No execution errors recorded.")
    return lines


def summary_to_dicts(groups: list[GroupSummary]) -> list[dict]:
    """JSON-ready summary groups, as printed by ``summary --json``."""
    out = []
    for g in groups:
        completed = sum(g.status_counts[s] for s in COMPLETED_RUN_STATUSES)
        out.append(
            {
                "sequence_name": g.sequence_name,
                "config_hash": g.config_hash,
                "status_counts": {s.value: n for s, n in g.status_counts.items()},
                "completed_runs": completed,
                "run_yield_percent": run_yield(g.status_counts[RunStatus.PASS], completed),
                "failure_pareto": [r.__dict__ for r in pareto(g.failures_by_step)],
                "errors_by_category": dict(g.errors_by_category),
            }
        )
    return out


def summary_to_json(groups: list[GroupSummary]) -> str:
    return json.dumps(summary_to_dicts(groups), indent=2)
