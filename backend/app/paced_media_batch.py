"""Paced WhatsApp media campaigns — spread N sends over 24h/48h with human-like jitter."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import SessionLocal
from app.models import CampaignMedia, CampaignRun, User, WhatsAppInstance

logger = logging.getLogger("crm.paced_media")

_stop = asyncio.Event()
_task: asyncio.Task | None = None
_wake = asyncio.Event()
# One active paced send pipeline per Evolution instance / agent (parallel across senders).
_active_sender_keys: set[str] = set()
_sender_tasks: dict[str, asyncio.Task] = {}

PACED_HOURS_ALLOWED = {24, 36, 48}
MIN_GAP_SECONDS = 45.0
# Never schedule more than this many hours even if model invents a weird value.
MAX_TOTAL_HOURS = 48
MIN_PACED_HOURS = 12

IST = ZoneInfo("Asia/Kolkata")
# Human sleeping hours (India) — no outbound paced sends in this window.
SLEEP_START = time(23, 45)
SLEEP_END = time(5, 0)
LEADS_MAX_SENDS_PER_IST_DAY = 250
# Wait for Evolution line to return after a socket flap (see INSTANCE_RECONNECT_*).
INSTANCE_RECONNECT_MAX_WAIT_SECONDS = 45 * 60
INSTANCE_RECONNECT_POLL_INITIAL_S = 3.0
INSTANCE_RECONNECT_POLL_MAX_S = 30.0


def parse_total_hours(args: dict[str, Any]) -> int | None:
    """Return paced window hours (12-48) when requested; else None.

    Prefers exact 24/36/48. Other values in range are kept (e.g. 30 -> 30).
    Values under 12 snap up to 24.
    """
    raw = args.get("total_hours")
    if raw is None or raw == "":
        raw = args.get("hours") or args.get("pace_hours") or args.get("window_hours")
    if raw is not None and str(raw).strip() != "":
        try:
            hours = int(float(str(raw).strip().lower().replace("h", "").replace("hours", "")))
        except (TypeError, ValueError):
            hours = 0
        if hours in PACED_HOURS_ALLOWED:
            return hours
        if MIN_PACED_HOURS <= hours <= MAX_TOTAL_HOURS:
            return hours
        if 1 <= hours < MIN_PACED_HOURS:
            return 24
    pace = str(args.get("pace") or args.get("pacing") or "").strip().lower()
    if pace in {"slow", "human", "human-like", "human_like", "paced", "spread"}:
        return 24
    return None


def plan_jitter_gaps(n: int, total_hours: float) -> list[float]:
    """N-1 gaps that sum ≈ total_hours*3600 with random human-like intervals (no instant spam)."""
    if n <= 1:
        return []
    total_s = max(float(total_hours) * 3600.0, MIN_GAP_SECONDS * (n - 1))
    slots = n - 1
    avg = total_s / slots
    min_gap = max(MIN_GAP_SECONDS, avg * 0.35)
    max_gap = max(min_gap + 1.0, avg * 1.85)
    if min_gap * slots > total_s:
        min_gap = max(30.0, total_s / slots * 0.55)
        max_gap = max(min_gap + 1.0, total_s / slots * 1.45)

    gaps: list[float] = []
    remaining_slots = slots
    remaining_time = total_s
    for _ in range(slots):
        if remaining_slots == 1:
            gaps.append(max(min_gap, remaining_time))
            break
        max_here = min(max_gap, remaining_time - min_gap * (remaining_slots - 1))
        min_here = min(min_gap, max_here)
        gap = random.uniform(min_here, max(min_here, max_here))
        gaps.append(float(gap))
        remaining_time -= gap
        remaining_slots -= 1
    return gaps


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ist_now() -> datetime:
    return datetime.now(IST)


def _run_filter_meta(run: CampaignRun) -> dict[str, Any]:
    try:
        meta = json.loads(run.filter_meta_json or "{}")
    except json.JSONDecodeError:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _is_leads_audience_run(run: CampaignRun) -> bool:
    meta = _run_filter_meta(run)
    return (meta.get("source") or "").strip().lower() == "leads"


def in_ist_sleep_window(dt: datetime | None = None) -> bool:
    """True between 23:45 and 05:00 IST (inclusive start, exclusive end at 05:00)."""
    local = (dt or ist_now()).astimezone(IST)
    t = local.time()
    if t >= SLEEP_START:
        return True
    if t < SLEEP_END:
        return True
    return False


def _ist_combine(day, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=IST)


def seconds_until_ist_wake(dt: datetime | None = None) -> float:
    """Seconds until paced sends may resume (05:00 IST)."""
    local = (dt or ist_now()).astimezone(IST)
    if not in_ist_sleep_window(local):
        return 0.0
    t = local.time()
    if t >= SLEEP_START:
        wake_day = local.date() + timedelta(days=1)
    else:
        wake_day = local.date()
    wake = _ist_combine(wake_day, SLEEP_END)
    return max(0.0, (wake - local).total_seconds())


def seconds_until_next_ist_day_send_start(dt: datetime | None = None) -> float:
    """After Leads 250/day cap — wait until 05:00 IST on the next calendar day."""
    local = (dt or ist_now()).astimezone(IST)
    next_day = local.date() + timedelta(days=1)
    start = _ist_combine(next_day, SLEEP_END)
    return max(0.0, (start - local).total_seconds())


def _parse_item_sent_at_ist(item: dict[str, Any]) -> datetime | None:
    raw = item.get("sent_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    except (TypeError, ValueError):
        return None


def count_leads_sent_on_ist_day(recipients: list[Any], day: date) -> int:
    n = 0
    for item in recipients:
        if not isinstance(item, dict) or item.get("status") != "sent":
            continue
        sent_local = _parse_item_sent_at_ist(item)
        if sent_local is None:
            continue
        if sent_local.date() == day:
            n += 1
    return n


def count_leads_sent_ist_today(recipients: list[Any]) -> int:
    return count_leads_sent_on_ist_day(recipients, ist_now().date())


def seconds_until_send_allowed(recipients: list[Any], run: CampaignRun) -> float:
    """Combined wait: sleeping hours + Leads 250/day hard cap."""
    wake_s = seconds_until_ist_wake()
    if wake_s > 0:
        return wake_s
    if _is_leads_audience_run(run) and count_leads_sent_ist_today(recipients) >= LEADS_MAX_SENDS_PER_IST_DAY:
        return seconds_until_next_ist_day_send_start()
    return 0.0


async def _sender_instance_connected(db, run: CampaignRun) -> bool:
    pid = str(run.from_instance_public_id or "").strip()
    if not pid:
        return False
    row = (
        await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.public_id == pid))
    ).scalar_one_or_none()
    return bool(row and (row.connection_status or "").strip().lower() == "connected")


async def _wait_for_sender_instance_connected(db, run: CampaignRun, timeout_s: float) -> bool:
    """Poll WhatsAppInstance.connection_status until Connected or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_s)
    poll_s = INSTANCE_RECONNECT_POLL_INITIAL_S
    while loop.time() < deadline and not _stop.is_set():
        await db.commit()
        if await _sender_instance_connected(db, run):
            return True
        chunk = min(poll_s, max(0.0, deadline - loop.time()))
        if chunk <= 0.05:
            break
        try:
            await asyncio.wait_for(_stop.wait(), timeout=chunk)
            return False
        except asyncio.TimeoutError:
            poll_s = min(INSTANCE_RECONNECT_POLL_MAX_S, poll_s * 1.45)
    return await _sender_instance_connected(db, run)


async def _wait_send_slot(run: CampaignRun, recipients: list[Any]) -> bool:
    """Block until outside sleep window and under Leads daily cap. False if stop requested."""
    while not _stop.is_set():
        wait_s = seconds_until_send_allowed(recipients, run)
        if wait_s <= 0.5:
            return True
        chunk = min(wait_s, 60.0)
        logger.info(
            "paced run %s holding %.0fs (IST sleep / leads %s per day)",
            run.id,
            wait_s,
            LEADS_MAX_SENDS_PER_IST_DAY,
        )
        try:
            await asyncio.wait_for(_stop.wait(), timeout=chunk)
            return False
        except asyncio.TimeoutError:
            continue
    return False


def runtime_gap_seconds(run: CampaignRun, idx: int) -> float:
    """Human-like random gap after recipient idx (extra jitter for Leads pacing)."""
    is_leads = _is_leads_audience_run(run)
    try:
        gaps = json.loads(run.gaps_json or "[]")
    except json.JSONDecodeError:
        gaps = []
    if isinstance(gaps, list) and idx < len(gaps) and float(gaps[idx] or 0) > 0:
        base = float(gaps[idx])
    else:
        th = max(int(run.total_hours or 0), MIN_PACED_HOURS)
        n = max(int(run.total_count or 1), idx + 1)
        base = max(MIN_GAP_SECONDS, (th * 3600.0) / max(n, 1))
    if is_leads:
        lo = max(MIN_GAP_SECONDS, base * 0.55)
        hi = max(lo + 15.0, base * 2.4)
        return random.uniform(lo, hi)
    return max(MIN_GAP_SECONDS, base * random.uniform(0.82, 1.28))


def _planned_gap_base_seconds(run: CampaignRun, idx: int) -> float:
    try:
        gaps = json.loads(run.gaps_json or "[]")
    except json.JSONDecodeError:
        gaps = []
    if isinstance(gaps, list) and idx < len(gaps) and float(gaps[idx] or 0) > 0:
        return float(gaps[idx])
    th = max(int(run.total_hours or 0), MIN_PACED_HOURS)
    n = max(int(run.total_count or 1), idx + 1)
    return max(MIN_GAP_SECONDS, (th * 3600.0) / max(n, 1))


def mean_runtime_gap_seconds(run: CampaignRun, idx: int) -> float:
    """Deterministic mean gap (same bounds as runtime_gap_seconds random jitter)."""
    base = _planned_gap_base_seconds(run, idx)
    if _is_leads_audience_run(run):
        lo = max(MIN_GAP_SECONDS, base * 0.55)
        hi = max(lo + 15.0, base * 2.4)
        return (lo + hi) / 2.0
    return max(MIN_GAP_SECONDS, base * 1.05)


def _seconds_until_send_allowed_at(
    recipients: list[Any],
    run: CampaignRun,
    dt: datetime,
    simulated_by_day: dict[date, int] | None = None,
) -> float:
    """IST sleep + Leads 250/day wait from dt (optional projected sends per IST day)."""
    local = dt.astimezone(IST)
    wake_s = seconds_until_ist_wake(local)
    if wake_s > 0:
        return wake_s
    if _is_leads_audience_run(run):
        day = local.date()
        sim = (simulated_by_day or {}).get(day, 0)
        if count_leads_sent_on_ist_day(recipients, day) + sim >= LEADS_MAX_SENDS_PER_IST_DAY:
            return seconds_until_next_ist_day_send_start(local)
    return 0.0


def estimate_paced_run_end_at(run: CampaignRun) -> datetime | None:
    """Project finish time for a running/queued paced run (IST rules, mean gaps)."""
    status = (run.status or "").strip().lower()
    if status not in {"running", "queued"}:
        return None

    try:
        recipients = json.loads(run.recipients_json or "[]")
    except json.JSONDecodeError:
        recipients = []
    if not isinstance(recipients, list):
        recipients = []

    remaining = max(
        0,
        int(run.total_count or 0)
        - int(run.success_count or 0)
        - int(run.failed_count or 0)
        - int(run.skipped_count or 0),
    )
    if remaining <= 0:
        return None

    total_hours = int(run.total_hours or 0)
    if total_hours <= 0 and not _is_leads_audience_run(run):
        return None

    t = ist_now()
    wait_s = _seconds_until_send_allowed_at(recipients, run, t)
    if wait_s > 0:
        t = t + timedelta(seconds=wait_s)

    idx_start = max(0, int(run.next_index or 0))
    simulated_by_day: dict[date, int] = {}

    for i in range(remaining):
        while True:
            wait_s = _seconds_until_send_allowed_at(recipients, run, t, simulated_by_day)
            if wait_s <= 0:
                break
            t = t + timedelta(seconds=wait_s)

        if _is_leads_audience_run(run):
            day = t.astimezone(IST).date()
            simulated_by_day[day] = simulated_by_day.get(day, 0) + 1

        if i < remaining - 1:
            t = t + timedelta(seconds=mean_runtime_gap_seconds(run, idx_start + i))

    return t


async def refresh_run_recipients_from_audience(db, run: CampaignRun) -> tuple[list[dict[str, Any]], int]:
    """Append pending recipients when a custom audience grows (live filter resolve)."""
    from app.ai.whatsapp_send_guards import phone_key
    from app.audiences import resolve_audience_members
    from app.models import CustomAudience

    meta = _run_filter_meta(run)
    if meta.get("filter") != "audience":
        try:
            recipients = json.loads(run.recipients_json or "[]")
        except json.JSONDecodeError:
            recipients = []
        return recipients if isinstance(recipients, list) else [], 0

    aud_id = meta.get("audience_id")
    if aud_id is None:
        return [], 0
    row = await db.get(CustomAudience, int(aud_id))
    if not row:
        return [], 0

    try:
        recipients = json.loads(run.recipients_json or "[]")
    except json.JSONDecodeError:
        recipients = []
    if not isinstance(recipients, list):
        recipients = []

    members, total, with_phone = await resolve_audience_members(
        db, row, limit=1500, with_phone_only=True
    )
    existing_keys: set[str] = set()
    for r in recipients:
        if not isinstance(r, dict):
            continue
        k = phone_key(str(r.get("to_phone") or ""))
        if k:
            existing_keys.add(k)

    added = 0
    for m in members:
        phone = (m.phone or "").strip()
        if not phone:
            continue
        pk = phone_key(phone)
        if not pk or pk in existing_keys:
            continue
        existing_keys.add(pk)
        recipients.append(
            {
                "to_phone": phone,
                "customer_name": (m.name or phone)[:160],
                "status": "pending",
                "error": "",
            }
        )
        added += 1

    if added:
        run.total_count = len(recipients)
        run.recipients_json = json.dumps(recipients, ensure_ascii=False)
        meta["total_with_phone"] = with_phone
        meta["total"] = total
        meta["dynamic_refresh"] = True
        run.filter_meta_json = json.dumps(meta, ensure_ascii=False)[:8000]
        logger.info(
            "paced run %s audience refresh +%s recipients (queue=%s, live_with_phone=%s)",
            run.id,
            added,
            len(recipients),
            with_phone,
        )
    return recipients, added


async def create_paced_campaign_run(
    db,
    *,
    user: User,
    batch_id: str,
    media_id: int,
    media_title: str,
    media_uid: int | None,
    audience_label: str,
    agent_name: str,
    from_instance_public_id: str,
    caption: str,
    recipients: list[dict[str, str]],
    total_hours: int,
    filter_meta: dict[str, Any] | None = None,
    confirm_summary: str = "",
) -> CampaignRun:
    """Persist a running Campaign Report row + recipient queue for the background worker."""
    from app.campaign_reports import allocate_campaign_run_uid, format_media_display_name

    media_row = await db.get(CampaignMedia, media_id)
    media_name = format_media_display_name(
        media_row, fallback=media_title or (str(media_uid) if media_uid else "")
    )
    gaps = plan_jitter_gaps(len(recipients), total_hours)
    avg_gap = (sum(gaps) / len(gaps)) if gaps else 0.0
    note = (
        f"Paced send scheduled over {total_hours}h "
        f"({len(recipients)} recipients, ~{int(avg_gap // 60)}m avg gap with jitter). "
        f"{confirm_summary}"
    )[:4000]
    queue = [
        {
            "to_phone": r.get("to_phone") or "",
            "customer_name": r.get("customer_name") or "",
            "status": "pending",
            "error": "",
        }
        for r in recipients
    ]
    run_uid = await allocate_campaign_run_uid(db)
    row = CampaignRun(
        campaign_id=None,
        uid=run_uid,
        batch_id=(batch_id or "")[:80],
        tool_name="send_whatsapp_media_batch",
        agent_name=(agent_name or "AI Agent")[:200],
        media_name=media_name[:240],
        media_id=media_id,
        audience_name=(audience_label or "")[:200],
        total_count=len(recipients),
        success_count=0,
        failed_count=0,
        skipped_count=0,
        replied_count=0,
        from_instance_public_id=(from_instance_public_id or "")[:80],
        note=note,
        occurred_at=_now(),
        status="running",
        total_hours=int(total_hours),
        next_index=0,
        recipients_json=json.dumps(queue, ensure_ascii=False),
        gaps_json=json.dumps(gaps),
        caption=(caption or "")[:4000],
        user_id=user.id if user else None,
        started_at=_now(),
        finished_at=None,
        filter_meta_json=json.dumps(filter_meta or {}, ensure_ascii=False)[:8000],
    )
    db.add(row)
    await db.flush()
    return row


def wake_paced_worker() -> None:
    _wake.set()


# Recipient statuses that must never be re-blasted on Restart / Resume.
_KEEP_STATUSES = frozenset({"sent", "skipped"})
# Campaign Report statuses eligible for Restart / Resume-on-new-instance.
RESUMABLE_RUN_STATUSES = frozenset({"failed", "stopped"})


def prepare_recipients_for_resume(
    recipients: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep SUCCESS/skipped; reset failed (and other non-terminal) to pending.

    Returns (queue, stats) where stats has success/skipped/failed/pending/next_index.
    Already-sent numbers are never reset — prevents double-send on restart.
    """
    out: list[dict[str, Any]] = []
    success = 0
    skipped = 0
    pending = 0
    first_pending: int | None = None

    for i, raw in enumerate(recipients):
        if isinstance(raw, dict):
            item = dict(raw)
        else:
            item = {"to_phone": "", "customer_name": "", "status": "pending", "error": ""}
        status = str(item.get("status") or "").strip().lower()
        if status == "sent":
            success += 1
            out.append(item)
            continue
        if status == "skipped":
            skipped += 1
            out.append(item)
            continue
        # failed / pending / unknown → retry as pending
        item["status"] = "pending"
        item["error"] = ""
        pending += 1
        if first_pending is None:
            first_pending = i
        out.append(item)

    next_index = first_pending if first_pending is not None else len(out)
    return out, {
        "success_count": success,
        "skipped_count": skipped,
        "failed_count": 0,
        "pending_count": pending,
        "next_index": next_index,
        "total_count": len(out),
    }


def _parse_run_recipients(run: CampaignRun) -> list[Any]:
    try:
        recipients = json.loads(run.recipients_json or "[]")
    except json.JSONDecodeError:
        recipients = []
    return recipients if isinstance(recipients, list) else []


async def resume_paced_campaign_run(
    db,
    run: CampaignRun,
    *,
    user: User,
    from_instance: WhatsAppInstance | None = None,
    mode: str = "restart",
) -> dict[str, Any]:
    """Re-queue a failed/stopped paced run from remaining unsent recipients.

    mode:
      - restart: same Evolution instance (must already be Connected)
      - resume_new_instance: rebind to ``from_instance`` then continue
    """
    status = (run.status or "").strip().lower()
    if status not in RESUMABLE_RUN_STATUSES:
        raise ValueError(
            f"Campaign is {status or 'unknown'} — only Failed/Stopped runs can restart "
            "(active campaigns are left running)."
        )

    recipients_raw = _parse_run_recipients(run)
    if not recipients_raw:
        raise ValueError("Campaign has no recipient queue to resume")

    queue, stats = prepare_recipients_for_resume(recipients_raw)
    pending = int(stats["pending_count"])
    if pending <= 0:
        raise ValueError("Nothing left to send — all recipients are already Success or Skipped")

    if from_instance is not None:
        conn = (from_instance.connection_status or "").strip().lower()
        if conn != "connected":
            raise ValueError(
                f"Instance '{from_instance.employee_name or from_instance.display_name}' "
                f"is {from_instance.connection_status or 'Disconnected'} — connect WhatsApp first"
            )
        agent = (from_instance.employee_name or from_instance.display_name or "agent").strip()
        run.from_instance_public_id = (from_instance.public_id or "")[:80]
        run.agent_name = agent[:200]
    else:
        # Restart on existing binding — require that line Connected again.
        pid = str(run.from_instance_public_id or "").strip()
        if not pid:
            raise ValueError("Campaign has no Evolution instance — use Resume on new instance")
        row = (
            await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.public_id == pid))
        ).scalar_one_or_none()
        if not row or (row.deleted_at is not None):
            raise ValueError("Original Evolution instance not found — use Resume on new instance")
        if (row.connection_status or "").strip().lower() != "connected":
            raise ValueError(
                f"Agent '{run.agent_name or row.display_name}' is not Connected — "
                "reconnect WhatsApp, then Restart, or Resume on a different instance"
            )

    # Ensure paced worker will pick this up (instant batches may have total_hours=0).
    if int(run.total_hours or 0) < MIN_PACED_HOURS:
        run.total_hours = 24

    hours = int(run.total_hours or 24)
    # Fresh jitter plan for the remaining window (human-like; does not affect already-sent).
    run.gaps_json = json.dumps(plan_jitter_gaps(max(1, pending), hours))

    run.recipients_json = json.dumps(queue, ensure_ascii=False)
    run.total_count = int(stats["total_count"])
    run.success_count = int(stats["success_count"])
    run.skipped_count = int(stats["skipped_count"])
    run.failed_count = 0
    run.next_index = int(stats["next_index"])
    run.status = "running"
    run.finished_at = None
    run.started_at = run.started_at or _now()
    if user and not run.user_id:
        run.user_id = user.id

    mode_label = "Restart" if mode == "restart" else "Resume on new instance"
    agent_label = run.agent_name or "agent"
    run.note = (
        f"{mode_label}: {pending} remaining via {agent_label} "
        f"(kept {stats['success_count']} success, {stats['skipped_count']} skipped; "
        f"failed reset). Paced {hours}h — next {run.next_index}/{run.total_count}."
    )[:4000]

    await db.flush()
    return {
        "ok": True,
        "mode": mode,
        "run_id": int(run.id),
        "uid": int(run.uid) if run.uid is not None else None,
        "status": run.status,
        "agent_name": run.agent_name,
        "from_instance_public_id": run.from_instance_public_id,
        "pending_count": pending,
        "success_count": int(run.success_count or 0),
        "skipped_count": int(run.skipped_count or 0),
        "failed_count": 0,
        "total_count": int(run.total_count or 0),
        "next_index": int(run.next_index or 0),
        "total_hours": hours,
        "note": run.note,
    }


async def _process_one_run(run_id: int) -> bool:
    """Send the next pending recipient for a paced run. Returns True if more work remains."""
    from app.ai.tools import (
        _is_fatal_batch_send_error,
        _is_recoverable_paced_sender_outage,
        tool_send_whatsapp_media,
    )

    async with SessionLocal() as db:
        run = await db.get(CampaignRun, run_id)
        if not run or (run.status or "") not in {"queued", "running"}:
            return False
        try:
            recipients = json.loads(run.recipients_json or "[]")
        except json.JSONDecodeError:
            recipients = []
        if not isinstance(recipients, list):
            recipients = []
        recipients, _ = await refresh_run_recipients_from_audience(db, run)
        if not recipients:
            run.status = "failed"
            run.note = ((run.note or "") + " · Empty recipient queue").strip(" ·")[:4000]
            run.finished_at = _now()
            await db.commit()
            return False

        if not await _wait_send_slot(run, recipients):
            await db.commit()
            return False

        idx = int(run.next_index or 0)
        if idx >= len(recipients):
            recipients, added_now = await refresh_run_recipients_from_audience(db, run)
            if added_now:
                await db.commit()
                return True
        if idx >= len(recipients):
            run.status = "completed"
            run.finished_at = _now()
            run.note = (
                f"Paced send finished: {run.success_count}/{run.total_count} sent, "
                f"{run.failed_count} failed, {run.skipped_count} skipped "
                f"over {run.total_hours}h."
            )[:4000]
            try:
                from app.campaign_reports import refresh_campaign_run_replied_count

                await refresh_campaign_run_replied_count(db, run)
            except Exception:  # noqa: BLE001
                logger.exception("paced replied_count finish refresh failed for run %s", run_id)
            await db.commit()
            return False

        user = await db.get(User, run.user_id) if run.user_id else None
        if not user:
            run.status = "failed"
            run.note = ((run.note or "") + " · Missing user for paced send").strip(" ·")[:4000]
            run.finished_at = _now()
            await db.commit()
            return False

        item = recipients[idx] if isinstance(recipients[idx], dict) else {}
        phone = str(item.get("to_phone") or "").strip()
        name = str(item.get("customer_name") or "").strip()

        async def _mark_recipient_skipped(reason: str) -> bool:
            """Mark current idx skipped and advance. Returns True if more work remains."""
            item["status"] = "skipped"
            item["error"] = reason
            run.skipped_count = int(run.skipped_count or 0) + 1
            recipients[idx] = item
            run.recipients_json = json.dumps(recipients, ensure_ascii=False)
            run.next_index = idx + 1
            run.status = "running"
            run.note = (
                f"Paced {run.total_hours}h: {run.success_count}/{run.total_count} sent, "
                f"{run.failed_count} failed, {run.skipped_count} skipped "
                f"(next {run.next_index}/{run.total_count})."
            )[:4000]
            await db.commit()
            if run.next_index >= len(recipients):
                run.status = "completed"
                run.finished_at = _now()
                run.note = (
                    f"Paced send finished: {run.success_count}/{run.total_count} sent, "
                    f"{run.failed_count} failed, {run.skipped_count} skipped "
                    f"over {run.total_hours}h."
                )[:4000]
                await db.commit()
                return False
            gap_s = runtime_gap_seconds(run, idx)
            try:
                await asyncio.wait_for(_stop.wait(), timeout=gap_s)
                return False
            except asyncio.TimeoutError:
                return True

        if phone:
            from app.ai.whatsapp_send_guards import phone_marked_spam

            if await phone_marked_spam(db, phone):
                return await _mark_recipient_skipped(
                    "Excluded: phone marked spam (WhatsApp conversation)"
                )

        if run.media_id is not None and phone:
            from app.ai.whatsapp_send_guards import (
                load_phones_with_prior_campaign_media,
                phone_key,
            )

            prior_keys = await load_phones_with_prior_campaign_media(
                db,
                campaign_media_id=int(run.media_id),
                exclude_run_id=int(run.id),
            )
            if phone_key(phone) in prior_keys:
                return await _mark_recipient_skipped(
                    "Excluded: same campaign media already sent/queued (any WhatsApp line)"
                )

        one_args = {
            "to_phone": phone,
            "customer_name": name,
            "media_id": run.media_id,
            "caption": run.caption or "",
            "from_instance_public_id": run.from_instance_public_id,
            "confirm_send": True,
            "dry_run": False,
        }
        # Evolution send_media already retries WA CDN 503; keep a short paced-level
        # retry so a single blip does not burn a recipient slot.
        from app.evolution_service import is_transient_media_upload_error

        one: dict[str, Any] = {}
        err = ""
        disconnect_deadline: float | None = None
        disconnect_wait_exhausted = False
        loop = asyncio.get_running_loop()

        while True:
            one = {}
            for attempt in range(1, 3):
                try:
                    one = await tool_send_whatsapp_media(db, user, one_args)
                except Exception as exc:  # noqa: BLE001
                    one = {"ok": False, "sent": False, "error": f"Send failed: {exc}", "to_phone": phone}
                err_try = str(one.get("error") or "")
                if (
                    one.get("sent")
                    or one.get("skipped_duplicate")
                    or one.get("skipped_self")
                    or one.get("skipped_staff")
                    or one.get("skipped_spam")
                ):
                    break
                if attempt < 2 and is_transient_media_upload_error(err_try):
                    logger.warning(
                        "paced run %s idx=%s transient media error — extra retry: %s",
                        run_id,
                        idx,
                        err_try[:200],
                    )
                    await asyncio.sleep(8.0)
                    continue
                break

            err = str(one.get("error") or "")
            if (
                one.get("sent")
                or one.get("skipped_duplicate")
                or one.get("skipped_self")
                or one.get("skipped_staff")
                or one.get("skipped_spam")
            ):
                break
            if not _is_recoverable_paced_sender_outage(err):
                break

            now = loop.time()
            if disconnect_deadline is None:
                disconnect_deadline = now + float(INSTANCE_RECONNECT_MAX_WAIT_SECONDS)
            if now >= disconnect_deadline:
                disconnect_wait_exhausted = True
                logger.error(
                    "paced run %s sender outage exceeded %ss at idx=%s: %s",
                    run_id,
                    INSTANCE_RECONNECT_MAX_WAIT_SECONDS,
                    idx,
                    err[:200],
                )
                break

            remaining = int(disconnect_deadline - now)
            run.status = "running"
            run.note = (
                f"Paced {run.total_hours}h: waiting for sender Connected "
                f"({remaining}s left) — {err[:160]}"
            )[:4000]
            await db.commit()
            if _stop.is_set():
                return False

            wait_s = min(
                60.0,
                max(INSTANCE_RECONNECT_POLL_INITIAL_S, disconnect_deadline - now),
            )
            if await _wait_for_sender_instance_connected(db, run, wait_s):
                logger.info("paced run %s sender Connected — retry idx=%s", run_id, idx)
                continue

        if disconnect_wait_exhausted:
            item["status"] = "failed"
            item["error"] = err[:400]
            recipients[idx] = item
            run.recipients_json = json.dumps(recipients, ensure_ascii=False)
            run.status = "failed"
            run.finished_at = _now()
            run.note = (
                f"Stopped: sender offline > {INSTANCE_RECONNECT_MAX_WAIT_SECONDS // 60} min "
                f"(idx {idx + 1}) — {err[:200]}"
            )[:4000]
            await db.commit()
            return False

        if one.get("sent"):
            item["status"] = "sent"
            item["sent_at"] = str(one.get("sent_at") or _now().isoformat())
            if one.get("conversation_id"):
                item["conversation_id"] = str(one.get("conversation_id"))
            if one.get("message_id"):
                item["message_id"] = str(one.get("message_id"))
            run.success_count = int(run.success_count or 0) + 1
        elif (
            one.get("skipped_duplicate")
            or one.get("skipped_self")
            or one.get("skipped_staff")
            or one.get("skipped_spam")
        ):
            item["status"] = "skipped"
            item["error"] = err[:400] or (
                "Excluded: phone marked spam (WhatsApp conversation)"
                if one.get("skipped_spam")
                else ""
            )
            run.skipped_count = int(run.skipped_count or 0) + 1
        else:
            item["status"] = "failed"
            item["error"] = err[:400]
            run.failed_count = int(run.failed_count or 0) + 1

        recipients[idx] = item
        run.recipients_json = json.dumps(recipients, ensure_ascii=False)
        run.next_index = idx + 1
        run.status = "running"
        run.note = (
            f"Paced {run.total_hours}h: {run.success_count}/{run.total_count} sent, "
            f"{run.failed_count} failed, {run.skipped_count} skipped "
            f"(next {run.next_index}/{run.total_count})."
        )[:4000]

        # Refresh replied cache periodically as the paced job progresses.
        if one.get("sent") and (int(run.success_count or 0) == 1 or int(run.success_count or 0) % 5 == 0):
            try:
                from app.campaign_reports import refresh_campaign_run_replied_count

                await refresh_campaign_run_replied_count(db, run)
            except Exception:  # noqa: BLE001
                logger.exception("paced replied_count refresh failed for run %s", run_id)

        # Per-recipient Work Report (non-blocking)
        try:
            import uuid

            from app.models import AiWorkReport

            status = "completed" if item["status"] in {"sent", "skipped"} else "failed"
            detail = f"To: {name or phone} · paced {run.total_hours}h · {item['status']}"
            if err:
                detail += f" · {err[:200]}"
            db.add(
                AiWorkReport(
                    public_id=str(uuid.uuid4()),
                    organization_id="yesweigh",
                    run_id=None,
                    tool_call_id=None,
                    user_id=user.id,
                    employee_id=None,
                    tool_name="send_whatsapp_media_batch",
                    task_name="Send WhatsApp media (paced)",
                    agent_name=run.agent_name or "",
                    task_details=detail[:4000],
                    status=status,
                    occurred_at=_now(),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("paced work report failed for run %s idx %s", run_id, idx)

        if _is_fatal_batch_send_error(err):
            run.status = "failed"
            run.finished_at = _now()
            run.note = (f"Stopped early (fatal): {err}"[:4000])
            await db.commit()
            return False

        if run.next_index >= len(recipients):
            run.status = "completed"
            run.finished_at = _now()
            run.note = (
                f"Paced send finished: {run.success_count}/{run.total_count} sent, "
                f"{run.failed_count} failed, {run.skipped_count} skipped "
                f"over {run.total_hours}h."
            )[:4000]
            try:
                from app.campaign_reports import refresh_campaign_run_replied_count

                await refresh_campaign_run_replied_count(db, run)
            except Exception:  # noqa: BLE001
                logger.exception("paced replied_count finish refresh failed for run %s", run_id)
            await db.commit()
            return False

        await db.commit()

        # Sleep with random human-like jitter before next send (outside transaction)
        gap_s = runtime_gap_seconds(run, idx)
        logger.info(
            "paced run %s sent idx=%s/%s; sleeping %.0fs",
            run_id,
            idx + 1,
            len(recipients),
            gap_s,
        )
        try:
            await asyncio.wait_for(_stop.wait(), timeout=gap_s)
            return False  # stop requested
        except asyncio.TimeoutError:
            return True


def _sender_key(run: CampaignRun) -> str:
    """Concurrency key: same Evolution phone/instance → FIFO; different → parallel.

    Prefer from_instance_public_id (actual WhatsApp line). Fall back to agent_name
    so two agents never block each other even if instance id is missing.
    """
    inst = (run.from_instance_public_id or "").strip()
    if inst:
        return f"inst:{inst}"
    agent = (run.agent_name or "").strip().lower()
    if agent:
        return f"agent:{agent}"
    uid = run.user_id
    if uid is not None:
        return f"user:{uid}"
    return f"run:{int(run.id)}"


def _run_has_pending(run: CampaignRun) -> bool:
    if (run.status or "") not in {"queued", "running"}:
        return False
    try:
        recipients = json.loads(run.recipients_json or "[]")
        if isinstance(recipients, list):
            return int(run.next_index or 0) < len(recipients)
    except json.JSONDecodeError:
        pass
    return int(run.next_index or 0) < int(run.total_count or 0)


async def _list_active_paced_runs() -> list[CampaignRun]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(CampaignRun)
                .where(CampaignRun.status.in_(("queued", "running")))
                .where(CampaignRun.tool_name == "send_whatsapp_media_batch")
                .where(CampaignRun.total_hours > 0)
                .order_by(CampaignRun.id.asc())
            )
        ).scalars().all()
        return [r for r in rows if _run_has_pending(r)]


async def _promote_queued(run_id: int) -> None:
    async with SessionLocal() as db:
        run = await db.get(CampaignRun, run_id)
        if run and (run.status or "") == "queued":
            run.status = "running"
            run.started_at = run.started_at or _now()
            await db.commit()


async def _claim_next_for_sender(sender_key: str) -> int | None:
    """FIFO next unfinished paced run for this sender (same instance/agent)."""
    rows = await _list_active_paced_runs()
    for row in rows:
        if _sender_key(row) != sender_key:
            continue
        await _promote_queued(int(row.id))
        return int(row.id)
    return None


async def _claim_runs_for_idle_senders() -> list[tuple[str, int]]:
    """One FIFO run per sender that is not already being processed."""
    rows = await _list_active_paced_runs()
    claimed: list[tuple[str, int]] = []
    seen_in_pass: set[str] = set()
    for row in rows:
        key = _sender_key(row)
        if key in _active_sender_keys or key in seen_in_pass:
            continue
        run_id = int(row.id)
        await _promote_queued(run_id)
        claimed.append((key, run_id))
        seen_in_pass.add(key)
    return claimed


async def _sender_pipeline(sender_key: str, run_id: int) -> None:
    """Process one sender's paced campaigns FIFO until that sender has no work left."""
    try:
        current = run_id
        while not _stop.is_set() and current is not None:
            more = await _process_one_run(current)
            if more:
                continue
            if _stop.is_set():
                break
            current = await _claim_next_for_sender(sender_key)
            if current is not None:
                logger.info(
                    "paced sender %s advancing to next FIFO run_id=%s",
                    sender_key,
                    current,
                )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("paced sender pipeline error key=%s run=%s", sender_key, run_id)
        await asyncio.sleep(5)
    finally:
        _active_sender_keys.discard(sender_key)
        _sender_tasks.pop(sender_key, None)
        _wake.set()


async def _loop() -> None:
    logger.info("Paced media batch worker started (per-sender concurrency)")
    while not _stop.is_set():
        try:
            # Drop finished task handles
            for key, task in list(_sender_tasks.items()):
                if task.done():
                    _active_sender_keys.discard(key)
                    _sender_tasks.pop(key, None)
                    try:
                        exc = task.exception()
                        if exc:
                            logger.error("paced sender task %s failed: %s", key, exc)
                    except (asyncio.CancelledError, asyncio.InvalidStateError):
                        pass

            claims = await _claim_runs_for_idle_senders()
            for key, run_id in claims:
                if key in _active_sender_keys:
                    continue
                _active_sender_keys.add(key)
                task = asyncio.create_task(
                    _sender_pipeline(key, run_id),
                    name=f"paced_sender:{key[:48]}",
                )
                _sender_tasks[key] = task
                logger.warning(
                    "paced sender pipeline started key=%s run_id=%s (active=%s)",
                    key,
                    run_id,
                    len(_active_sender_keys),
                )

            if not _sender_tasks:
                _wake.clear()
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
            else:
                # Brief poll so new agents (e.g. 113 while 112 runs) start promptly
                _wake.clear()
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("paced media worker loop error")
            await asyncio.sleep(5)
    logger.info("Paced media batch worker stopped")


def start_paced_media_worker() -> None:
    global _task
    if _task and not _task.done():
        return
    _stop.clear()
    _wake.set()
    _active_sender_keys.clear()
    _sender_tasks.clear()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Paced media worker not started — no running event loop")
        return
    _task = loop.create_task(_loop(), name="paced_media_batch")
    logger.warning("Paced media worker task created (per-sender)")
    print("Paced media worker task created (per-sender)", flush=True)


async def stop_paced_media_worker() -> None:
    global _task
    _stop.set()
    _wake.set()
    for t in list(_sender_tasks.values()):
        t.cancel()
    if _sender_tasks:
        await asyncio.gather(*_sender_tasks.values(), return_exceptions=True)
    _sender_tasks.clear()
    _active_sender_keys.clear()
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
