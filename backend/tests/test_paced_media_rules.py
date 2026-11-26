"""Paced campaign rules: IST sleep window, Leads 250/day, jitter helpers."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.paced_media_batch import (
    LEADS_MAX_SENDS_PER_IST_DAY,
    count_leads_sent_ist_today,
    estimate_paced_run_end_at,
    in_ist_sleep_window,
    mean_runtime_gap_seconds,
    runtime_gap_seconds,
    seconds_until_ist_wake,
    seconds_until_send_allowed,
)

IST = ZoneInfo("Asia/Kolkata")


def test_ist_sleep_window():
    awake = datetime(2026, 11, 23, 10, 0, tzinfo=IST)
    assert not in_ist_sleep_window(awake)
    late = datetime(2026, 11, 23, 23, 50, tzinfo=IST)
    assert in_ist_sleep_window(late)
    early = datetime(2026, 11, 24, 2, 30, tzinfo=IST)
    assert in_ist_sleep_window(early)
    boundary = datetime(2026, 11, 24, 5, 0, tzinfo=IST)
    assert not in_ist_sleep_window(boundary)


def test_seconds_until_ist_wake_from_night():
    dt = datetime(2026, 11, 23, 23, 50, tzinfo=IST)
    s = seconds_until_ist_wake(dt)
    assert 300 <= s <= 320  # ~10 min to 05:00 next day


def test_leads_daily_cap_blocks_when_full():
    run = MagicMock()
    run.filter_meta_json = '{"filter":"audience","source":"leads","audience_id":12}'
    today = datetime.now(IST).strftime("%Y-%m-%dT10:00:00+05:30")
    recipients = [
        {"status": "sent", "sent_at": today}
        for _ in range(LEADS_MAX_SENDS_PER_IST_DAY)
    ]
    assert count_leads_sent_ist_today(recipients) == LEADS_MAX_SENDS_PER_IST_DAY
    assert seconds_until_send_allowed(recipients, run) > 0


def test_dealers_skip_leads_daily_cap():
    run = MagicMock()
    run.filter_meta_json = '{"filter":"audience","source":"dealers","audience_id":11}'
    assert seconds_until_send_allowed([], run) == 0.0


def test_runtime_gap_jitter():
    run = MagicMock()
    run.filter_meta_json = '{"source":"leads"}'
    run.gaps_json = "[]"
    run.total_hours = 36
    run.total_count = 400
    gaps = [runtime_gap_seconds(run, i) for i in range(20)]
    assert all(g >= 45 for g in gaps)
    assert max(gaps) > min(gaps)


def test_mean_runtime_gap_within_jitter_bounds():
    run = MagicMock()
    run.filter_meta_json = '{"source":"dealers"}'
    run.gaps_json = "[]"
    run.total_hours = 24
    run.total_count = 100
    m = mean_runtime_gap_seconds(run, 5)
    assert m >= 45


def test_estimate_end_for_running_leads_campaign():
    run = MagicMock()
    run.status = "running"
    run.filter_meta_json = '{"filter":"audience","source":"leads","audience_id":12}'
    run.gaps_json = "[]"
    run.total_hours = 36
    run.total_count = 1410
    run.success_count = 190
    run.failed_count = 18
    run.skipped_count = 4
    run.next_index = 212
    run.recipients_json = "[]"
    end = estimate_paced_run_end_at(run)
    assert end is not None
    assert end > datetime.now(IST)