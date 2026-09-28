from __future__ import annotations

import pytest

from mini_sequencer.models import Limits, RunStatus, StepStatus
from mini_sequencer.sequencer import aggregate_status, evaluate

P, F, E, S = StepStatus.PASS, StepStatus.FAIL, StepStatus.ERROR, StepStatus.SKIP


@pytest.mark.parametrize(
    ("value", "expected"),
    [(4.75, P), (5.25, P), (5.0, P), (4.7499999, F), (5.2500001, F)],
)
def test_two_sided_limits_are_inclusive(value, expected):
    assert evaluate(value, Limits(min=4.75, max=5.25)) is expected


@pytest.mark.parametrize(("value", "expected"), [(120.0, P), (-5.0, P), (120.0001, F)])
def test_max_only(value, expected):
    assert evaluate(value, Limits(max=120.0)) is expected


@pytest.mark.parametrize(("value", "expected"), [(15.0, P), (1e9, P), (14.9999, F)])
def test_min_only(value, expected):
    assert evaluate(value, Limits(min=15.0)) is expected


def test_unrounded_value_is_compared():
    # 5.25004 displays as 5.25 but is outside the limit.
    assert evaluate(5.25004, Limits(max=5.25)) is F


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([P, P, P], RunStatus.PASS),
        ([P, F, P], RunStatus.FAIL),
        ([F, E, S], RunStatus.ERROR),
        ([E, F], RunStatus.ERROR),
        ([F, S, S], RunStatus.FAIL),
        ([], RunStatus.PASS),
    ],
)
def test_status_precedence(statuses, expected):
    assert aggregate_status(statuses) is expected
