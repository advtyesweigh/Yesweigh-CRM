"""Observe HTTP API. 404 when intelligence is disabled. No send, no task creation."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.inbox import _authorized_instance_ids
from app.intel.flags import can_rebuild, can_view_staff_reply, intel_access, intel_is_enabled
from app.intel.health import snapshot_for_dealer
from app.intel.jobs import enqueue_job
from app.intel.row_sort import sort_intel_dict_rows
from app.intel.queries import (
    IST,
    OBSERVE_TYPES,
    _dealers_by_ids,
    _employees_by_ids,
    _enrich_conversation_row,
    _instances_by_ids,
    _timestamp_fields,
    collect_findings,
    funnel_snapshot,
    service_queue,
    stamping_queue,
)
from app.models import AiCommitment, AiJob, User, WhatsAppConversation, WhatsAppReplyEvent
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter
from datetime import date, datetime, timezone

router = APIRouter(prefix="/api/intel", tags=["intel"])

Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


class ServiceThreadOut(BaseModel):
    """Service queue row. No advice and no procedure field."""

    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    customer_name: str = ""
    tags: list[str] = []
    severity: str = "high"
    last_message_at: str | None = None
    href: str = ""
    dealer_name: str = ""
    phone: str = ""
    agent_name: str = ""
    priority: str = ""
    since_at: str | None = None
    since_ist: str | None = None
    age_seconds: int | None = None
    age_label: str = ""
    time_till_label: str = ""
    avatar_url: str = ""
    summary: str = ""
    status: str = ""
    ai_status: str = ""
    ai_tag: str = ""


async def _gate(db: AsyncSession, user: User) -> str:
    if not await intel_is_enabled(db):
        raise HTTPException(status_code=404, detail="Intelligence is not enabled")
    access = intel_access(user)
    if access is None:
        raise HTTPException(status_code=403, detail="Management access required")
    return access


def _types_for(access: str, requested: str) -> list[str]:
    allowed = list(OBSERVE_TYPES) if access == "full" else ["unresolved_service"]
    if requested:
        want = [p.strip() for p in requested.split(",") if p.strip()]
        return [t for t in want if t in allowed]
    return allowed


async def _instances(db: AsyncSession, user: User) -> set[int] | None:
    allowed = await _authorized_instance_ids(db, user)
    if allowed is None:
        return None
    return set(allowed)


@router.get("/findings")
async def list_findings(
    db: Db,
    user: CurrentUser,
    type: str = Query("", description="Comma-separated finding types"),
    severity: str = "",
    dealer_id: int | None = None,
    limit: int = Query(80, ge=1, le=200),
) -> dict[str, Any]:
    access = await _gate(db, user)
    types = _types_for(access, type)
    payload = await collect_findings(
        db,
        types=types,
        severity=severity,
        dealer_id=dealer_id,
        allowed_instance_ids=await _instances(db, user),
        limit=limit,
    )
    return {
        "phase": "observe",
        "gemini": False,
        "access": access,
        "can_rebuild": can_rebuild(user),
        "counts": payload["counts"],
        "items": payload["items"],
        "note": "Figures are SQL. Wording is not generated in Observe.",
    }


@router.get("/commitments")
async def list_commitments(
    db: Db,
    user: CurrentUser,
    limit: int = Query(300, ge=1, le=300),
) -> dict[str, Any]:
    access = await _gate(db, user)
    allowed = await _instances(db, user)
    clauses = [
        WhatsAppConversation.organization_id == "yesweigh",
        newsletter_list_filter(),
        dm_inbox_filter(),
        AiCommitment.owner_side == "staff",
        AiCommitment.status.in_(("open", "missed")),
    ]
    if allowed is not None:
        clauses.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed or {-1}))
    if access == "service":
        clauses.append(func.lower(WhatsAppConversation.service_category) == "service")
    rows = (
        await db.execute(
            select(AiCommitment, WhatsAppConversation)
            .join(WhatsAppConversation, AiCommitment.conversation_id == WhatsAppConversation.id)
            .where(*clauses)
            .order_by(AiCommitment.id.desc())
            .limit(limit)
        )
    ).all()
    convs = [conv for _, conv in rows]
    dealer_map = await _dealers_by_ids(db, {int(c.dealer_id) for c in convs if c.dealer_id})
    emp_map = await _employees_by_ids(db, {int(c.assigned_employee_id) for c in convs if c.assigned_employee_id})
    inst_map = await _instances_by_ids(db, {int(c.whatsapp_instance_id) for c in convs})
    now = datetime.now(timezone.utc)
    from app.intel.dedupe import dedupe_intel_rows, is_blast_summary

    items = []
    for row, conv in rows:
        promise = (row.promise_text or "").strip()
        if is_blast_summary(promise):
            continue
        confidence = row.confidence or "low"
        enriched = _enrich_conversation_row(
            conv,
            dealer=dealer_map.get(int(conv.dealer_id or 0)),
            emp=emp_map.get(int(conv.assigned_employee_id or 0)),
            inst=inst_map.get(int(conv.whatsapp_instance_id)),
            now=now,
            status=row.status or "open",
            ai_tag="Commitment",
        )
        due_fields = _timestamp_fields(
            datetime.combine(row.due_on, datetime.min.time(), tzinfo=IST) if row.due_on else None,
            now=now,
        )
        items.append(
            {
                "id": row.public_id,
                "promise_text": row.promise_text,
                "due_on": row.due_on.isoformat() if row.due_on else None,
                "owner_side": row.owner_side,
                "status": row.status,
                "confidence": confidence,
                "high_lane": confidence != "low",
                "extractor": row.extractor,
                "contact_name": conv.customer_name or conv.customer_phone or "",
                "href": f"/whatsapp?thread={conv.public_id}",
                "conversation_id": conv.public_id,
                "customer_jid": conv.customer_jid or "",
                "summary": (promise or enriched.get("summary") or "")[:240],
                "priority": "High" if confidence != "low" else "Low",
                "due_ist": due_fields.get("since_ist"),
                **{k: enriched.get(k, "") for k in (
                    "dealer_name",
                    "phone",
                    "agent_name",
                    "avatar_url",
                    "since_at",
                    "since_ist",
                    "age_seconds",
                    "age_label",
                    "time_till_label",
                    "ai_status",
                    "ai_tag",
                )},
            }
        )
    # One commitment row per WhatsApp conversation / phone (keep best summary).
    items = dedupe_intel_rows(items, drop_blast=True)
    return {
        "items": sort_intel_dict_rows(items),
        "note": "Low-confidence promises stay off the high lane until a person confirms them. This does not change unreplied counts.",
    }


@router.get("/dealers/{dealer_id}/health")
async def dealer_health(dealer_id: int, db: Db, user: CurrentUser) -> dict[str, Any]:
    access = await _gate(db, user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Management access required")
    snap = await snapshot_for_dealer(db, dealer_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Dealer not found")
    return snap


@router.get("/funnel")
async def funnel(
    db: Db,
    user: CurrentUser,
    items: bool = Query(True, description="Include row payloads (false = counts/ids only)"),
    stage: str = Query(
        "",
        description="When set, hydrate items only for this funnel key "
        "(enquiry|sales_order|invoice_open|invoice_settled)",
    ),
) -> dict[str, Any]:
    access = await _gate(db, user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Management access required")
    want_stage = (stage or "").strip().lower()
    stage_alias = {
        "enquiry": "enquiry",
        "sales_order": "sales_order",
        "waiting": "invoice_open",
        "invoice_open": "invoice_open",
        "paid": "invoice_settled",
        "invoice_settled": "invoice_settled",
    }
    item_stages: set[str] | None = None
    include_items = bool(items)
    if want_stage:
        mapped = stage_alias.get(want_stage)
        if mapped is None:
            raise HTTPException(status_code=400, detail="Unknown funnel stage")
        item_stages = {mapped}
        include_items = True
    elif not items:
        include_items = False
        item_stages = set()
    payload = await funnel_snapshot(
        db,
        id_limit=40,
        enquiry_id_limit=120,
        include_items=include_items,
        item_stages=item_stages,
    )
    payload["note"] = "Missing stages are omitted. There is no dispatch stage."
    return payload


@router.get("/service")
async def service(db: Db, user: CurrentUser, limit: int = Query(300, ge=1, le=300)) -> dict[str, Any]:
    await _gate(db, user)
    rows = await service_queue(
        db,
        allowed_instance_ids=await _instances(db, user),
        limit=limit,
    )
    items = [ServiceThreadOut.model_validate(row).model_dump() for row in rows]
    return {
        "items": items,
        "note": "Tags only. No calibration, firmware, or metrology procedure.",
    }


@router.get("/stamping")
async def stamping(db: Db, user: CurrentUser, limit: int = Query(300, ge=1, le=300)) -> dict[str, Any]:
    access = await _gate(db, user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Management access required")
    rows = await stamping_queue(
        db,
        allowed_instance_ids=await _instances(db, user),
        limit=limit,
    )
    items = [ServiceThreadOut.model_validate(row).model_dump() for row in rows]
    return {
        "items": items,
        "note": "OV / seal / Certificate of Verification WhatsApp chats.",
    }


class StageMoveIn(BaseModel):
    conversation_id: str
    to_stage: str
    from_stage: str = ""


@router.post("/stage-move")
async def stage_move(body: StageMoveIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Move a WhatsApp chat to another Intelligence stage (or Closed). Stores training signal."""
    await _gate(db, user)
    from app.intel.stage_move import STAGE_LABELS, move_conversation_stage, normalize_stage

    try:
        normalize_stage(body.to_stage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = await move_conversation_stage(
            db,
            user=user,
            conversation_public_id=body.conversation_id.strip(),
            to_stage=body.to_stage,
            from_stage=body.from_stage,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result["stages"] = [{"key": k, "label": v} for k, v in STAGE_LABELS.items()]
    return result


@router.get("/stage-options")
async def stage_options(db: Db, user: CurrentUser) -> dict[str, Any]:
    await _gate(db, user)
    from app.intel.stage_move import STAGE_LABELS

    return {"stages": [{"key": k, "label": v} for k, v in STAGE_LABELS.items()]}


@router.get("/staff-reply")
async def staff_reply(
    db: Db,
    user: CurrentUser,
    period: str = Query("7d"),
) -> dict[str, Any]:
    await _gate(db, user)
    if not can_view_staff_reply(user):
        raise HTTPException(status_code=403, detail="Management access required")
    from app.whatsapp_reply_analytics import aggregate_reply_analytics

    block = await aggregate_reply_analytics(db, period=period, allowed_instance_ids=None)
    events = (
        await db.execute(
            select(WhatsAppReplyEvent, WhatsAppConversation.public_id, WhatsAppConversation.customer_name)
            .join(WhatsAppConversation, WhatsAppReplyEvent.conversation_id == WhatsAppConversation.id)
            .where(WhatsAppReplyEvent.organization_id == "yesweigh")
            .order_by(WhatsAppReplyEvent.replied_at.desc())
            .limit(40)
        )
    ).all()
    links = [
        {
            "id": evt.id,
            "conversation_id": public_id,
            "customer_name": name or "",
            "replied_at": evt.replied_at.isoformat() if evt.replied_at else None,
            "reply_seconds": int(evt.reply_seconds or 0),
            "office_seconds": int(evt.office_seconds or 0),
            "off_seconds": int(evt.off_seconds or 0),
            "href": f"/whatsapp?thread={public_id}",
        }
        for evt, public_id, name in events
    ]
    return {
        "notice": "This is response-time evidence, not an appraisal.",
        "reply_stats_href": "/whatsapp/reply-stats",
        "analytics": block,
        "reply_events": links,
    }


@router.post("/jobs/rebuild")
async def rebuild(db: Db, user: CurrentUser) -> dict[str, Any]:
    await _gate(db, user)
    if not can_rebuild(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    fingerprint = date.today().isoformat()
    job, created = await enqueue_job(
        db,
        job_type="rebuild_findings",
        subject_type="organization",
        subject_id="yesweigh",
        fingerprint=fingerprint,
        priority=200,
    )
    await db.commit()
    return {"public_id": job.public_id, "created": created, "status": job.status}


@router.get("/jobs")
async def jobs(db: Db, user: CurrentUser) -> dict[str, Any]:
    await _gate(db, user)
    if not can_rebuild(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    rows = (
        await db.execute(
            select(AiJob.status, func.count()).group_by(AiJob.status)
        )
    ).all()
    return {"counts": {status: int(n or 0) for status, n in rows}}
