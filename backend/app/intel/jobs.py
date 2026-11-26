"""Postgres job queue for intelligence. Concurrency 1. Idle when the flag is off.

Webhook callers use ``schedule_thread_intel``, which inserts after the message
transaction and never calls Gemini or Sarvam.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.intel.extract import (
    apply_secondary_intents,
    conversation_is_intel_subject,
    detect_secondary_intents,
    enqueue_decision,
    extract_commitment_from_message,
    idempotency_key,
    next_commitment_status,
)
from app.intel.flags import ORG, intel_is_enabled
from app.intel.health import rebuild_dealer_health
from app.intel.queries import OBSERVE_TYPES, collect_findings
from app.models import (
    AiCommitment,
    AiFinding,
    AiJob,
    AiThreadInsight,
    WhatsAppConversation,
    WhatsAppMessage,
)

logger = logging.getLogger("yesweigh.intel")

_stop = asyncio.Event()
_task: asyncio.Task | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_job(
    db: AsyncSession,
    *,
    job_type: str,
    subject_type: str,
    subject_id: str,
    fingerprint: str,
    priority: int = 100,
) -> tuple[AiJob, bool]:
    key = idempotency_key(job_type, subject_type, subject_id, fingerprint)
    existing = (
        await db.execute(select(AiJob).where(AiJob.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    job = AiJob(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        job_type=job_type,
        subject_type=subject_type,
        subject_id=str(subject_id),
        idempotency_key=key,
        input_fingerprint=fingerprint[:128],
        status="queued",
        priority=priority,
    )
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(select(AiJob).where(AiJob.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        raise
    return job, True


async def enqueue_thread(db: AsyncSession, conversation_id: int) -> bool:
    """Insert a rule-extract job for one DM. No-op when the flag is off or the chat is a group."""
    if not await intel_is_enabled(db):
        return False
    conv = await db.get(WhatsAppConversation, conversation_id)
    if conv is None or not enqueue_decision(enabled=True, jid=conv.customer_jid):
        return False
    last_id = (
        await db.execute(
            select(WhatsAppMessage.id)
            .where(WhatsAppMessage.conversation_id == conv.id)
            .order_by(WhatsAppMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    fingerprint = f"{last_id or 0}|{(conv.ai_outline_fingerprint or '')[:40]}"
    await enqueue_job(
        db,
        job_type="refresh_thread_insight",
        subject_type="whatsapp_conversation",
        subject_id=str(conv.id),
        fingerprint=fingerprint,
        priority=100,
    )
    return True


def schedule_thread_intel(*conversation_ids: int) -> None:
    """Fire-and-forget insert. Failures are logged. The webhook has already committed."""
    ids = [int(i) for i in conversation_ids if i]
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_enqueue_threads_safe(ids))


async def _enqueue_threads_safe(ids: list[int]) -> None:
    try:
        async with SessionLocal() as db:
            if not await intel_is_enabled(db):
                return
            for cid in ids:
                await enqueue_thread(db, cid)
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("intel enqueue skipped: %s", exc)


async def ensure_daily_rebuild(db: AsyncSession) -> None:
    fingerprint = date.today().isoformat()
    await enqueue_job(
        db,
        job_type="rebuild_findings",
        subject_type="organization",
        subject_id=ORG,
        fingerprint=fingerprint,
        priority=200,
    )
    await enqueue_job(
        db,
        job_type="rebuild_dealer_health",
        subject_type="organization",
        subject_id=ORG,
        fingerprint=fingerprint,
        priority=200,
    )
    await enqueue_job(
        db,
        job_type="mark_missed_commitments",
        subject_type="organization",
        subject_id=ORG,
        fingerprint=fingerprint,
        priority=200,
    )


async def claim_next(db: AsyncSession) -> AiJob | None:
    now = _now()
    job = (
        await db.execute(
            select(AiJob)
            .where(
                or_(
                    AiJob.status == "queued",
                    and_(
                        AiJob.status == "leased",
                        AiJob.lease_until.is_not(None),
                        AiJob.lease_until < now,
                    ),
                ),
                or_(AiJob.not_before.is_(None), AiJob.not_before <= now),
            )
            .order_by(AiJob.priority.asc(), AiJob.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = "leased"
    job.lease_until = now + timedelta(seconds=180)
    job.attempts = int(job.attempts or 0) + 1
    await db.flush()
    return job


async def _persist_findings(db: AsyncSession) -> int:
    payload = await collect_findings(
        db,
        types=list(OBSERVE_TYPES),
        allowed_instance_ids=None,
        limit=300,
    )
    now = _now()
    written = 0
    seen: set[tuple[str, str, str, str]] = set()
    for item in payload["items"]:
        sources = item.get("sources") or []
        if not sources:
            continue
        key = (
            item["finding_type"],
            item["subject_type"],
            str(item["subject_id"]),
            item.get("fingerprint") or "open",
        )
        seen.add(key)
        row = (
            await db.execute(
                select(AiFinding).where(
                    AiFinding.finding_type == key[0],
                    AiFinding.subject_type == key[1],
                    AiFinding.subject_id == key[2],
                    AiFinding.fingerprint == key[3],
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = AiFinding(
                public_id=str(uuid.uuid4()),
                organization_id=ORG,
                finding_type=key[0],
                subject_type=key[1],
                subject_id=key[2],
                fingerprint=key[3],
            )
            db.add(row)
        if row.status == "dismissed":
            continue
        row.dealer_id = item.get("dealer_id")
        row.conversation_id = item.get("conversation_id")
        row.severity = item.get("severity") or "medium"
        row.title = item.get("title") or ""
        row.detail_json = json.dumps(item.get("detail") or {}, default=str)
        row.source_json = json.dumps(sources)
        row.generated_by = item.get("generated_by") or "sql"
        row.status = "open"
        row.resolved_at = None
        written += 1
    # Only close rows for a type when this pass loaded the full set (count <= fetched).
    fetched_counts: dict[str, int] = {}
    for key in seen:
        fetched_counts[key[0]] = fetched_counts.get(key[0], 0) + 1
    complete_types = {
        ftype
        for ftype, total in (payload.get("counts") or {}).items()
        if fetched_counts.get(ftype, 0) >= int(total or 0)
    }
    open_rows = (
        await db.execute(select(AiFinding).where(AiFinding.status == "open", AiFinding.resolved_at.is_(None)))
    ).scalars().all()
    for row in open_rows:
        key = (row.finding_type, row.subject_type, row.subject_id, row.fingerprint)
        if row.finding_type in complete_types and key not in seen:
            row.resolved_at = now
    await db.flush()
    return written


async def _refresh_thread(db: AsyncSession, conversation_id: int) -> None:
    conv = await db.get(WhatsAppConversation, conversation_id)
    if conv is None or not conversation_is_intel_subject(conv.customer_jid):
        return
    messages = (
        await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.conversation_id == conv.id)
            .order_by(WhatsAppMessage.id.desc())
            .limit(30)
        )
    ).scalars().all()
    messages = list(reversed(messages))
    parts = [(m.body or "") + "\n" + (m.transcript or "") for m in messages]
    parts.append(conv.ai_outline or "")
    intents = detect_secondary_intents(*parts)
    primary, secondary = apply_secondary_intents(conv, intents)
    insight = (
        await db.execute(select(AiThreadInsight).where(AiThreadInsight.conversation_id == conv.id))
    ).scalar_one_or_none()
    if insight is None:
        insight = AiThreadInsight(conversation_id=conv.id)
        db.add(insight)
    # primary_category is a copy. service_category on the conversation is not written.
    _ = primary
    insight.primary_category = (conv.service_category or "")[:20]
    insight.secondary_intents = json.dumps(secondary)
    insight.sentiment_label = "unclear"
    insight.outline_fingerprint = (conv.ai_outline_fingerprint or "")[:128]
    from app.intel.enquiry_list import rewrite_outline_if_needed, store_cache, verdict_for
    from app.whatsapp_outline import message_text_for_outline

    text_rows = [
        {"direction": m.direction or "", "text": message_text_for_outline(m) or ""}
        for m in messages
    ]
    verdict = await verdict_for(db, conv, text_rows, allow_gemini=True)
    rewrite_outline_if_needed(conv, verdict)
    store_cache(insight, conv, verdict)
    today = date.today()
    for msg in messages:
        hit = extract_commitment_from_message(
            body=msg.body or "",
            transcript=msg.transcript or "",
            direction=msg.direction or "",
            today=today,
            message_id=msg.id,
            conversation_id=conv.id,
        )
        if hit is None:
            continue
        existing = (
            await db.execute(
                select(AiCommitment).where(
                    AiCommitment.conversation_id == conv.id,
                    AiCommitment.fingerprint == hit["fingerprint"],
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(
            AiCommitment(
                public_id=str(uuid.uuid4()),
                organization_id=ORG,
                conversation_id=conv.id,
                dealer_id=conv.dealer_id,
                promise_text=hit["promise_text"],
                due_on=hit["due_on"],
                owner_side=hit["owner_side"],
                status="open",
                evidence_message_id=hit["evidence_message_id"],
                extractor="rule",
                confidence=hit["confidence"],
                fingerprint=hit["fingerprint"],
            )
        )
    await db.flush()


async def mark_missed_commitments(db: AsyncSession, today: date | None = None) -> int:
    """Past-due open rows become missed. Does not read or write reply flags."""
    current = today or date.today()
    rows = (
        await db.execute(
            select(AiCommitment).where(
                AiCommitment.status == "open",
                AiCommitment.due_on.is_not(None),
                AiCommitment.due_on < current,
            )
        )
    ).scalars().all()
    changed = 0
    for row in rows:
        nxt = next_commitment_status(row.status, row.due_on, current)
        if nxt != row.status:
            row.status = nxt
            changed += 1
    if changed:
        await db.flush()
    return changed


async def run_job(db: AsyncSession, job: AiJob) -> None:
    if job.job_type == "rebuild_findings":
        await _persist_findings(db)
        return
    if job.job_type == "rebuild_dealer_health":
        await rebuild_dealer_health(db)
        return
    if job.job_type == "mark_missed_commitments":
        await mark_missed_commitments(db)
        return
    if job.job_type in {"refresh_thread_insight", "extract_commitments"}:
        try:
            cid = int(job.subject_id)
        except (TypeError, ValueError):
            return
        await _refresh_thread(db, cid)
        return
    if job.job_type == "backfill_tick":
        # Observe does not backfill history on deploy.
        return


async def _fail_or_retry(job: AiJob, exc: Exception) -> None:
    job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
    if int(job.attempts or 0) >= int(job.max_attempts or 3):
        job.status = "failed"
        job.finished_at = _now()
    else:
        job.status = "queued"
        job.not_before = _now() + timedelta(seconds=30 * int(job.attempts or 1))
        job.lease_until = None


async def _worker_loop() -> None:
    logger.info("Intel worker started")
    while not _stop.is_set():
        try:
            async with SessionLocal() as db:
                if not await intel_is_enabled(db):
                    await db.rollback()
                else:
                    await ensure_daily_rebuild(db)
                    job = await claim_next(db)
                    if job is None:
                        await db.commit()
                    else:
                        job_id = job.id
                        try:
                            await run_job(db, job)
                            job.status = "done"
                            job.finished_at = _now()
                            job.last_error = ""
                            await db.commit()
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("intel job %s failed", getattr(job, "public_id", job_id))
                            await db.rollback()
                            async with SessionLocal() as db2:
                                failed = await db2.get(AiJob, job_id)
                                if failed is not None:
                                    await _fail_or_retry(failed, exc)
                                    await db2.commit()
        except Exception:  # noqa: BLE001
            logger.exception("intel worker loop error")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            pass
    logger.info("Intel worker stopped")


def start_intel_worker() -> None:
    global _task
    _stop.clear()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Intel worker not started — no running event loop")
        return
    if _task and not _task.done():
        return
    _task = loop.create_task(_worker_loop())


async def stop_intel_worker() -> None:
    global _task
    _stop.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            _task.cancel()
        _task = None
