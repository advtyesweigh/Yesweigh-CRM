"""All-India run orchestration — one state job at a time; resume after restart."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_finder.all_india import (
    ALL_INDIA_MODE_FULL,
    ALL_INDIA_MODE_PILOT,
    assert_state_allowed,
    full_india_state_queue,
)
from app.dealer_finder.constants import (
    ORG_DEFAULT,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_PENDING,
    STATUS_RUNNING,
)
from app.dealer_finder.jobs import create_search_job, job_public, wake_dealer_finder_worker
from app.dealer_finder.settings_svc import get_or_create_settings
from app.models import DealerAllIndiaRun, DealerSearchJob, User

logger = logging.getLogger("crm.dealer_finder.all_india")

TERMINAL_JOB = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_PAUSED})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append_log(run: DealerAllIndiaRun, message: str) -> None:
    try:
        lines = json.loads(run.logs_json or "[]")
        if not isinstance(lines, list):
            lines = []
    except json.JSONDecodeError:
        lines = []
    ts = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"{ts} {message}")
    run.logs_json = json.dumps(lines[-400:], ensure_ascii=False)


def run_public(run: DealerAllIndiaRun) -> dict[str, Any]:
    try:
        states = json.loads(run.states_json or "[]")
    except json.JSONDecodeError:
        states = []
    try:
        skipped = json.loads(run.skipped_states_json or "[]")
    except json.JSONDecodeError:
        skipped = []
    return {
        "id": run.id,
        "public_id": run.public_id,
        "mode": run.mode,
        "status": run.status,
        "state_index": run.state_index,
        "states_total": len(states) if isinstance(states, list) else 0,
        "states_completed": run.completed_states,
        "skipped_states": skipped if isinstance(skipped, list) else [],
        "pilot_state": run.pilot_state,
        "pilot_district": run.pilot_district,
        "active_job_public_id": run.active_job_public_id,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "logs": json.loads(run.logs_json or "[]") if run.logs_json else [],
    }


async def create_pilot_run(
    db: AsyncSession,
    *,
    user: User,
    state: str,
    district: str,
    categories: list[str] | None = None,
    organization_id: str = ORG_DEFAULT,
) -> DealerAllIndiaRun:
    st = assert_state_allowed(state)
    district = (district or "").strip()
    if not district:
        raise ValueError("pilot district is required")
    settings = await get_or_create_settings(db, organization_id=organization_id)
    active = (
        await db.execute(
            select(DealerAllIndiaRun).where(
                DealerAllIndiaRun.organization_id == organization_id,
                DealerAllIndiaRun.status.in_([STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED]),
            )
        )
    ).scalar_one_or_none()
    if active:
        raise ValueError(f"An all-India run is already active ({active.public_id})")

    run = DealerAllIndiaRun(
        public_id=str(uuid.uuid4()),
        organization_id=organization_id,
        mode=ALL_INDIA_MODE_PILOT,
        status=STATUS_PENDING,
        states_json=json.dumps([st], ensure_ascii=False),
        state_index=0,
        pilot_state=st,
        pilot_district=district,
        created_by=user.id,
    )
    _append_log(run, f"Pilot queued: {district}, {st}")
    db.add(run)
    await db.flush()

    job = await create_search_job(
        db,
        user=user,
        state=st,
        categories=categories,
        district=district,
        search_depth="full",
        organization_id=organization_id,
    )
    run.active_job_public_id = job.public_id
    run.status = STATUS_RUNNING
    run.started_at = _now()
    _append_log(run, f"Started pilot job {job.public_id}")
    settings.pilot_state = st
    settings.pilot_district = district
    await db.flush()
    wake_dealer_finder_worker()
    return run


async def create_full_run(
    db: AsyncSession,
    *,
    user: User,
    categories: list[str] | None = None,
    organization_id: str = ORG_DEFAULT,
) -> DealerAllIndiaRun:
    settings = await get_or_create_settings(db, organization_id=organization_id)
    if not settings.pilot_district_completed:
        raise ValueError("Complete the pilot district run before full all-India")
    if not settings.all_india_enabled:
        raise ValueError("Admin has not enabled all-India discovery (settings.all_india_enabled)")

    active = (
        await db.execute(
            select(DealerAllIndiaRun).where(
                DealerAllIndiaRun.organization_id == organization_id,
                DealerAllIndiaRun.status.in_([STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED]),
            )
        )
    ).scalar_one_or_none()
    if active:
        raise ValueError(f"An all-India run is already active ({active.public_id})")

    queue = full_india_state_queue()
    if not queue:
        raise ValueError("No eligible states in queue")

    run = DealerAllIndiaRun(
        public_id=str(uuid.uuid4()),
        organization_id=organization_id,
        mode=ALL_INDIA_MODE_FULL,
        status=STATUS_PENDING,
        states_json=json.dumps(queue, ensure_ascii=False),
        state_index=0,
        created_by=user.id,
    )
    _append_log(run, f"Full all-India queued across {len(queue)} states (excluded TN/KL/PY)")
    db.add(run)
    await db.flush()
    await _kick_next_state(db, run, user=user, categories=categories)
    return run


async def _kick_next_state(
    db: AsyncSession,
    run: DealerAllIndiaRun,
    *,
    user: User | None,
    categories: list[str] | None = None,
) -> bool:
    """Start job for current state_index if not skipped. Returns False when finished."""
    try:
        states = json.loads(run.states_json or "[]")
    except json.JSONDecodeError:
        states = []
    try:
        skipped = json.loads(run.skipped_states_json or "[]")
    except json.JSONDecodeError:
        skipped = []
    skipped_set = {str(s) for s in skipped}

    idx = int(run.state_index or 0)
    while idx < len(states):
        st = assert_state_allowed(str(states[idx]))
        if st in skipped_set:
            _append_log(run, f"Skipped state {st}")
            idx += 1
            run.state_index = idx
            continue
        actor = user
        if actor is None and run.created_by:
            actor = await db.get(User, run.created_by)
        if actor is None:
            raise ValueError("Run missing created_by user")

        if run.mode == ALL_INDIA_MODE_PILOT:
            district = (run.pilot_district or "").strip()
            job = await create_search_job(
                db,
                user=actor,
                state=st,
                categories=categories,
                district=district,
                search_depth="full",
                organization_id=run.organization_id,
            )
        else:
            job = await create_search_job(
                db,
                user=actor,
                state=st,
                categories=categories,
                search_depth="full",
                organization_id=run.organization_id,
            )
        run.active_job_public_id = job.public_id
        run.status = STATUS_RUNNING
        run.started_at = run.started_at or _now()
        _append_log(run, f"Started state job {st} → {job.public_id}")
        await db.flush()
        wake_dealer_finder_worker()
        return True
    run.status = STATUS_COMPLETED
    run.finished_at = _now()
    run.active_job_public_id = ""
    _append_log(run, "All-India run completed")
    await db.flush()
    return False


async def tick_all_india_runs(db: AsyncSession) -> None:
    """Advance orchestration when the active child job finishes (worker hook)."""
    runs = (
        await db.execute(
            select(DealerAllIndiaRun).where(
                DealerAllIndiaRun.status.in_([STATUS_RUNNING, STATUS_PAUSED])
            )
        )
    ).scalars().all()
    for run in runs:
        if run.status == STATUS_PAUSED:
            continue
        pid = (run.active_job_public_id or "").strip()
        if not pid:
            continue
        job = (
            await db.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == pid)
            )
        ).scalar_one_or_none()
        if not job:
            continue
        if job.status not in TERMINAL_JOB:
            continue

        if job.status == STATUS_PAUSED:
            run.status = STATUS_PAUSED
            run.error = job.error or "Child job paused (budget)"
            _append_log(run, f"Paused with job {pid}: {run.error}")
            await db.flush()
            continue

        if run.mode == ALL_INDIA_MODE_PILOT and job.status == STATUS_COMPLETED:
            settings = await get_or_create_settings(db, organization_id=run.organization_id)
            settings.pilot_district_completed = True
            settings.pilot_state = run.pilot_state or job.state
            settings.pilot_district = run.pilot_district or job.district_filter
            _append_log(run, "Pilot district completed — admin may enable full all-India")

        if run.mode == ALL_INDIA_MODE_FULL and job.status == STATUS_COMPLETED:
            actor = await db.get(User, run.created_by) if run.created_by else None
            if actor:
                from app.dealer_finder.jobs import import_eligible_places

                try:
                    imp = await import_eligible_places(
                        db,
                        user=actor,
                        job_id=job.id,
                        with_phone_only=True,
                    )
                    st = (job.state or "").strip() or "?"
                    _append_log(
                        run,
                        f"Auto-import {st}: inserted={imp.get('inserted', 0)} "
                        f"dupes={imp.get('skipped_duplicates', 0)} "
                        f"no_phone={imp.get('skipped_no_phone', 0)} "
                        f"job_dupes={imp.get('skipped_job_phone_dupes', 0)}",
                    )
                except Exception as exc:  # noqa: BLE001
                    _append_log(run, f"Auto-import failed for {job.public_id}: {exc!s}")

        run.completed_states = int(run.completed_states or 0) + 1
        _append_log(
            run,
            f"State job {pid} finished as {job.status}; "
            f"{run.completed_states} state(s) done",
        )
        run.state_index = int(run.state_index or 0) + 1
        run.active_job_public_id = ""

        if run.mode == ALL_INDIA_MODE_PILOT:
            run.status = STATUS_COMPLETED if job.status == STATUS_COMPLETED else STATUS_FAILED
            run.finished_at = _now()
            if job.status == STATUS_FAILED:
                run.error = job.error or "Pilot job failed"
            await db.flush()
            continue

        await _kick_next_state(db, run, user=None)
    await db.commit()


async def pause_run(db: AsyncSession, run: DealerAllIndiaRun) -> DealerAllIndiaRun:
    run.status = STATUS_PAUSED
    _append_log(run, "Run paused by user")
    pid = (run.active_job_public_id or "").strip()
    if pid:
        job = (
            await db.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == pid)
            )
        ).scalar_one_or_none()
        if job and job.status == STATUS_RUNNING:
            from app.dealer_finder.jobs import set_job_status

            await set_job_status(db, job, STATUS_PAUSED)
    await db.flush()
    return run


async def resume_run(db: AsyncSession, run: DealerAllIndiaRun) -> DealerAllIndiaRun:
    run.status = STATUS_RUNNING
    run.error = ""
    _append_log(run, "Run resumed")
    pid = (run.active_job_public_id or "").strip()
    if pid:
        job = (
            await db.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == pid)
            )
        ).scalar_one_or_none()
        if job and job.status in {STATUS_PAUSED, STATUS_PENDING}:
            from app.dealer_finder.jobs import set_job_status

            job.error = ""
            await set_job_status(db, job, STATUS_PENDING)
    else:
        await _kick_next_state(db, run, user=None)
    await db.flush()
    wake_dealer_finder_worker()
    return run


async def cancel_run(db: AsyncSession, run: DealerAllIndiaRun) -> DealerAllIndiaRun:
    run.status = STATUS_CANCELLED
    run.finished_at = _now()
    _append_log(run, "Run cancelled")
    pid = (run.active_job_public_id or "").strip()
    if pid:
        job = (
            await db.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == pid)
            )
        ).scalar_one_or_none()
        if job and job.status not in {STATUS_COMPLETED, STATUS_CANCELLED}:
            from app.dealer_finder.jobs import set_job_status

            await set_job_status(db, job, STATUS_CANCELLED)
    await db.flush()
    return run


async def skip_current_state(db: AsyncSession, run: DealerAllIndiaRun) -> DealerAllIndiaRun:
    try:
        states = json.loads(run.states_json or "[]")
    except json.JSONDecodeError:
        states = []
    idx = int(run.state_index or 0)
    if idx >= len(states):
        return run
    st = str(states[idx])
    try:
        skipped = json.loads(run.skipped_states_json or "[]")
    except json.JSONDecodeError:
        skipped = []
    if st not in skipped:
        skipped.append(st)
    run.skipped_states_json = json.dumps(skipped, ensure_ascii=False)
    _append_log(run, f"Skipped state {st} by operator")

    pid = (run.active_job_public_id or "").strip()
    if pid:
        job = (
            await db.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == pid)
            )
        ).scalar_one_or_none()
        if job and job.status not in {STATUS_COMPLETED, STATUS_CANCELLED}:
            from app.dealer_finder.jobs import set_job_status

            await set_job_status(db, job, STATUS_CANCELLED)
    run.active_job_public_id = ""
    run.state_index = idx + 1
    await _kick_next_state(db, run, user=None)
    await db.flush()
    wake_dealer_finder_worker()
    return run


async def dashboard_snapshot(db: AsyncSession, *, organization_id: str = ORG_DEFAULT) -> dict[str, Any]:
    """State tiles: latest job status + place counts (excluded states omitted)."""
    from app.dealer_finder.all_india import eligible_states

    states = eligible_states()
    jobs = (
        await db.execute(
            select(DealerSearchJob)
            .where(DealerSearchJob.organization_id == organization_id)
            .order_by(DealerSearchJob.id.desc())
        )
    ).scalars().all()
    latest_by_state: dict[str, DealerSearchJob] = {}
    for j in jobs:
        st = (j.state or "").strip()
        if st and st not in latest_by_state and not is_excluded_import(st):
            latest_by_state[st] = j

    tiles: list[dict[str, Any]] = []
    for st in states:
        j = latest_by_state.get(st)
        tiles.append(
            {
                "state": st,
                "job_status": j.status if j else None,
                "job_public_id": j.public_id if j else None,
                "places_new": int(j.places_new or 0) if j else 0,
                "progress_pct": job_public(j).get("progress_pct", 0) if j else 0,
            }
        )
    run = (
        await db.execute(
            select(DealerAllIndiaRun)
            .where(DealerAllIndiaRun.organization_id == organization_id)
            .order_by(DealerAllIndiaRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "tiles": tiles,
        "active_run": run_public(run) if run else None,
    }


def is_excluded_import(st: str) -> bool:
    from app.dealer_finder.all_india import is_excluded_state

    return is_excluded_state(st)