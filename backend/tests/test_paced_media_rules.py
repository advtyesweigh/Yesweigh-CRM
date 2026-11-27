"""Paced campaign rules: IST sleep window, Leads daily cap, per-run overrides, jitter."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.paced_media_batch import (
    LEADS_MAX_SENDS_PER_IST_DAY,
    count_leads_sent_ist_today,
    daily_send_cap_for_run,
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
    still_asleep = datetime(2026, 11, 24, 5, 49, tzinfo=IST)
    assert in_ist_sleep_window(still_asleep)
    boundary = datetime(2026, 11, 24, 5, 50, tzinfo=IST)
    assert not in_ist_sleep_window(boundary)


def test_seconds_until_ist_wake_from_night():
    dt = datetime(2026, 11, 23, 23, 50, tzinfo=IST)
    s = seconds_until_ist_wake(dt)
    # 23:50 → 05:50 next day = 6h
    assert 6 * 3600 - 5 <= s <= 6 * 3600 + 5


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
    assert daily_send_cap_for_run(run) is None


def test_per_run_daily_cap_override():
    today = datetime.now(IST).date().isoformat()
    run = MagicMock()
    run.filter_meta_json = (
        '{"filter":"audience","source":"leads","audience_id":5,'
        f'"max_sends_per_ist_day":100,"max_sends_by_ist_day":{{"{today}":25}}}}'
    )
    assert daily_send_cap_for_run(run) == 25
    tomorrow = datetime.now(IST).date()
    from datetime import timedelta

    assert daily_send_cap_for_run(run, tomorrow + timedelta(days=1)) == 100

    recipients = [
        {
            "status": "sent",
            "sent_at": datetime.now(IST).strftime("%Y-%m-%dT10:00:00+05:30"),
        }
        for _ in range(25)
    ]
    assert seconds_until_send_allowed(recipients, run) > 0


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
