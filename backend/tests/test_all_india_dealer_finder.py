"""All-India dealer discovery gates and geography."""

from __future__ import annotations

from app.dealer_finder.all_india import (
    DEFAULT_PILOT_DISTRICT,
    DEFAULT_PILOT_STATE,
    assert_state_allowed,
    eligible_states,
    full_india_state_queue,
    is_excluded_state,
)


def test_excluded_states() -> None:
    assert is_excluded_state("Tamil Nadu")
    assert is_excluded_state("tamilnadu")
    assert is_excluded_state("Kerala")
    assert is_excluded_state("Keralam")
    assert is_excluded_state("Puducherry")
    assert not is_excluded_state("Rajasthan")
    assert not is_excluded_state("Karnataka")


def test_assert_state_allowed_blocks_tn() -> None:
    try:
        assert_state_allowed("Tamil Nadu")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "excluded" in str(exc).lower()


def test_excluded_states_still_canonical_for_manual_jobs() -> None:
    """Manual single-state jobs use canon_location_state, not assert_state_allowed."""
    from app.dealer_finder.normalize import canon_location_state

    assert canon_location_state("Kerala") == "Keralam"
    assert is_excluded_state("Keralam")
    try:
        assert_state_allowed("Keralam")
        raise AssertionError("expected ValueError for all-India queue")
    except ValueError:
        pass


def test_eligible_states_omit_south() -> None:
    eligible = eligible_states()
    assert "Tamil Nadu" not in eligible
    assert "Puducherry" not in eligible
    assert "Keralam" not in eligible
    assert "Rajasthan" in eligible


def test_full_queue_sorted_and_filtered() -> None:
    queue = full_india_state_queue()
    assert queue == sorted(queue, key=str.lower)
    for ex in ("Tamil Nadu", "Puducherry", "Keralam"):
        assert ex not in queue


def test_pilot_defaults_outside_excluded() -> None:
    assert not is_excluded_state(DEFAULT_PILOT_STATE)
    assert DEFAULT_PILOT_DISTRICT


def test_count_units_verified_plan() -> None:
    from app.dealer_finder.all_india import count_units_for_state

    plan = {"districts": [{"name": "Jaipur", "cities": ["Mansarovar"]}]}
    n = count_units_for_state(plan, state="Rajasthan", district_filter="Jaipur")
    assert n >= 7