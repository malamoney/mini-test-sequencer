"""Self-contained HTML results page, written by ``mini-seq html``.

The page is a single file with the results embedded and no external requests,
so it opens offline and can be archived or attached next to the database.
Summary figures come from :meth:`ResultStore.summarize`, the same numbers
``mini-seq summary`` prints; the page's script only draws them. Per-run rows
and readings come from :meth:`ResultStore.get_runs` with the same filter.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from mini_sequencer import __version__
from mini_sequencer.models import RunRecord, StepRecord
from mini_sequencer.reporting import describe_filter, summary_to_dicts
from mini_sequencer.storage import GroupSummary, SummaryFilter

TEMPLATE = "templates/results.html"
DATA_MARKER = "__RESULTS_JSON__"


def render_html(
    groups: list[GroupSummary],
    runs: list[RunRecord],
    flt: SummaryFilter,
    source: str,
    generated_at: str,
) -> str:
    """Return the results page for ``groups`` and ``runs`` as one HTML document."""
    summaries = summary_to_dicts(groups)
    for summary, group in zip(summaries, groups, strict=True):
        summary["first_started_at"] = group.first_started_at
        summary["last_started_at"] = group.last_started_at
    data = {
        "source": source,
        "filter": describe_filter(flt),
        "generated_at": generated_at,
        "version": __version__,
        "groups": summaries,
        "runs": [_run_dict(run) for run in runs],
    }
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False)
    # The data sits in a <script type="application/json"> block. Escaping every
    # "<" means no stored text (a serial, an error message) can close the block,
    # and the result is still valid JSON.
    payload = payload.replace("<", "\\u003c")
    template = files("mini_sequencer").joinpath(TEMPLATE).read_text(encoding="utf-8")
    return template.replace(DATA_MARKER, payload, 1)


def _run_dict(run: RunRecord) -> dict[str, Any]:
    identity = run.device_identity or {}
    return {
        "id": run.id,
        "serial": run.serial,
        "sequence": run.sequence_name,
        "hash": run.config_hash,
        "started": run.started_at,
        "duration": run.duration_s,
        "status": run.status.value,
        "profile": identity.get("profile"),
        "model": identity.get("model"),
        "firmware": identity.get("firmware"),
        "err": run.error_category,
        "msg": run.error_message,
        "steps": [_step_dict(step) for step in run.steps],
    }


def _step_dict(step: StepRecord) -> dict[str, Any]:
    return {
        "pos": step.position,
        "id": step.step_id,
        "unit": step.unit,
        "min": step.limit_min,
        "max": step.limit_max,
        "value": step.measurement,
        "status": step.status.value,
        "err": step.error_category,
        "msg": step.error_message,
    }
