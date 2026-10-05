from __future__ import annotations

import json
import re

from mini_sequencer.html_report import DATA_MARKER, render_html
from mini_sequencer.models import RunStatus, StepStatus
from mini_sequencer.storage import SummaryFilter
from tests.unit.test_storage import finished, make_step

GENERATED_AT = "2026-03-01T00:00:00.000000+00:00"


def embedded_data(page: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="results-data">(.*?)</script>', page, re.S
    )
    assert match, "data block missing"
    return json.loads(match.group(1))


def test_page_embeds_summary_and_runs(store):
    P, F = StepStatus.PASS, StepStatus.FAIL
    finished(store, "p1", RunStatus.PASS, [make_step(1, "a", P)])
    finished(store, "f1", RunStatus.FAIL, [make_step(1, "a", F)])
    finished(store, "e1", RunStatus.ERROR, error=("timeout", "slow"))
    flt = SummaryFilter()

    page = render_html(store.summarize(flt), store.get_runs(flt), flt, "r.sqlite", GENERATED_AT)

    assert page.startswith("<!doctype html>")
    assert DATA_MARKER not in page
    data = embedded_data(page)
    assert data["source"] == "r.sqlite"
    assert data["filter"] == "all runs"
    (group,) = data["groups"]
    assert group["completed_runs"] == 3
    assert group["status_counts"]["PASS"] == 1
    assert round(group["run_yield_percent"], 1) == 33.3
    assert group["failure_pareto"][0]["step_id"] == "a"
    assert group["errors_by_category"] == {"timeout": 1}
    assert group["first_started_at"] and group["last_started_at"]
    assert [r["id"] for r in data["runs"]] == ["e1", "f1", "p1"]  # same start: by run ID
    assert data["runs"][1]["steps"][0] == {  # f1
        "pos": 1,
        "id": "a",
        "unit": "V",
        "min": 1.0,
        "max": 2.0,
        "value": 1.5,
        "status": "FAIL",
        "err": None,
        "msg": None,
    }


def test_stored_text_cannot_close_the_data_block(store):
    hostile = "</script><script>alert(1)</script><!--"
    finished(store, "x1", RunStatus.ERROR, serial=hostile, error=("internal", hostile))
    flt = SummaryFilter()

    page = render_html(store.summarize(flt), store.get_runs(flt), flt, "r.sqlite", GENERATED_AT)

    assert hostile not in page
    data = embedded_data(page)
    assert data["runs"][0]["serial"] == hostile
    assert data["runs"][0]["msg"] == hostile


def test_page_with_no_runs(store):
    flt = SummaryFilter(sequence_name="none")
    data = embedded_data(render_html([], [], flt, "r.sqlite", GENERATED_AT))
    assert data["groups"] == [] and data["runs"] == []
    assert data["filter"] == "sequence none"
