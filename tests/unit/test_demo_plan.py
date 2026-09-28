from __future__ import annotations

from collections import Counter

import pytest

from mini_sequencer.demo import DEMO_MIX, plan_units, profile_counts
from mini_sequencer.simulator import PROFILES


def test_mix_uses_known_profiles():
    assert {name for name, _ in DEMO_MIX} <= set(PROFILES)


def test_profile_counts_for_100_units_match_weights():
    assert profile_counts(100) == dict(DEMO_MIX)


@pytest.mark.parametrize("units", [1, 7, 33, 100, 250])
def test_profile_counts_sum_to_units(units):
    assert sum(profile_counts(units).values()) == units


def test_plan_is_deterministic_per_seed():
    assert plan_units(50, seed=42) == plan_units(50, seed=42)
    assert plan_units(50, seed=42) != plan_units(50, seed=43)


def test_plan_serials_unique_and_profiles_match_counts():
    plan = plan_units(100, seed=7)
    assert len({u.serial for u in plan}) == 100
    assert Counter(u.profile for u in plan) == Counter(profile_counts(100))
