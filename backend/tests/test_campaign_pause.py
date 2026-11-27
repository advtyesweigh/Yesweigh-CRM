"""Pause / unpause paced campaigns — preserve progress & caps; Est. end shifts on resume."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.paced_media_batch import (
    PAUSABLE_RUN_STATUSES,
    UNPAUSABLE_RUN_STATUSES,
    estimate_paced_run_end_at,
    pause_paced_campaign_run,
    unpause_paced_campaign_run,
)

IST = ZoneInfo("Asia/Kolkata")


def _leads_run(**kwargs):
    run = MagicMock()
    run.id = 114
    run.uid = 114
    run.status = "running"
    run.filter_meta_json = (
        '{"filter":"audience","source":"leads","audience_id":14,'
        '"max_sends_per_ist_day":100}'
    )
    run.gaps_json = "[]"
    run.total_hours = 36
    run.total_count = 2273
    run.success_count = 25
    run.failed_count = 4
    run.skipped_count = 0
    run.next_index = 29
    run.recipients_json = '[{"status":"sent"},{"status":"pending"},{"status":"pending"}]'
    run.note = ""
    run.finished_at = None
    run.started_at = datetime.now(IST)
    run.user_id = 1
    for k, v in kwargs.items():
        setattr(run, k, v)
    return run


@pytest.mark.asyncio
async def test_pause_sets_paused_keeps_progress():
    run = _leads_run()
    next_before = run.next_index
    success_before = run.success_count
    meta_before = run.filter_meta_json
    gaps_before = run.gaps_json

    class _Db:
        async def flush(self):
            return None

    out = await pause_paced_campaign_run(_Db(), run, user=None)
    assert out["ok"] is True
    assert run.status == "paused"
    assert run.next_index == next_before
    assert run.success_count == success_before
    assert run.filter_meta_json == meta_before
    assert run.gaps_json == gaps_before
    assert "Paused" in (run.note or "")


@pytest.mark.asyncio
async def test_pause_rejects_non_running():
    run = _leads_run(status="completed")

    class _Db:
        async def flush(self):
            return None

    with pytest.raises(ValueError, match="only Running/Queued"):
        await pause_paced_campaign_run(_Db(), run, user=None)


@pytest.mark.asyncio
async def test_unpause_restores_running_without_resetting_caps():
    run = _leads_run(
        status="paused",
        recipients_json=(
            '[{"status":"sent"},{"status":"pending"},{"status":"pending"},'
            '{"status":"pending"}]'
        ),
        next_index=1,
        success_count=1,
        failed_count=0,
        skipped_count=0,
        total_count=4,
    )
    meta_before = run.filter_meta_json
    gaps_before = run.gaps_json
    next_before = run.next_index

    class _Db:
        async def flush(self):
            return None

    out = await unpause_paced_campaign_run(_Db(), run, user=None)
    assert out["ok"] is True
    assert run.status == "running"
    assert run.next_index == next_before
    assert run.filter_meta_json == meta_before
    assert run.gaps_json == gaps_before
    assert "Resumed from pause" in (run.note or "")


@pytest.mark.asyncio
async def test_unpause_rejects_running():
    run = _leads_run(status="running")

    class _Db:
        async def flush(self):
            return None

    with pytest.raises(ValueError, match="only Paused"):
        await unpause_paced_campaign_run(_Db(), run, user=None)


def test_estimate_end_none_while_paused():
    run = _leads_run(status="paused", recipients_json="[]")
    assert estimate_paced_run_end_at(run) is None


def test_estimate_end_reschedules_from_now_when_running():
    """After unpause, Est. end is projected from now (paused time excluded)."""
    run = _leads_run(
        status="running",
        recipients_json="[]",
        total_count=500,
        success_count=25,
        failed_count=0,
        skipped_count=0,
        next_index=25,
    )
    end = estimate_paced_run_end_at(run)
    assert end is not None
    assert end > datetime.now(IST)
    # Cap 100/day + sleep window → remaining ~475 takes multiple days
    assert end > datetime.now(IST) + timedelta(days=2)


def test_status_sets():
    assert "running" in PAUSABLE_RUN_STATUSES
    assert "queued" in PAUSABLE_RUN_STATUSES
    assert "paused" in UNPAUSABLE_RUN_STATUSES
