"""HTTP API for KB learning, corrections, reply assistant, auto-reply config, analytics."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import corrections as corrections_mod
from app.ai import kb_service
from app.ai import learning as learning_mod
from app.ai import reply_engine
from app.auth import get_current_user
from app.db import get_db
from app.models import User, WaAiSuggestion, WaAutoReplyConfig, WhatsAppConversation, WhatsAppInstance

router = APIRouter(prefix="/api/ai", tags=["ai-learning"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(status_code=403, detail="Admin or Management role required")


# ----- Knowledge Base -----


class KbArticleIn(BaseModel):
    title: str = Field(default="", max_length=240)
    question: str = Field(default="", max_length=4000)
    answer: str = Field(default="", max_length=8000)
    category: str = Field(default="other", max_length=60)
    product: str = Field(default="", max_length=160)
    keywords: str = Field(default="", max_length=2000)
    language: str = Field(default="en", max_length=20)
    status: str | None = Field(default=None, max_length=40)
    review_notes: str = Field(default="", max_length=2000)


class KbStatusIn(BaseModel):
    status: str = Field(max_length=40)
    notes: str = Field(default="", max_length=2000)
    title: str | None = None
    question: str | None = None
    answer: str | None = None
    category: str | None = None
    product: str | None = None
    keywords: str | None = None
    language: str | None = None


class LearnIn(BaseModel):
    limit: int = Field(default=5, ge=1, le=20)
    conversation_id: str | None = None
    force: bool = False


class CorrectionIn(BaseModel):
    field_type: str
    target_type: str
    target_id: str
    original_value: str = ""
    corrected_value: str
    reason: str = ""
    apply: bool = True


class SuggestIn(BaseModel):
    force: bool = False


class SuggestionPatch(BaseModel):
    reply_text: str | None = None
    feedback: str | None = None
    status: str | None = None


class AutoReplyPatch(BaseModel):
    mode: str | None = None
    auto_send_enabled: bool | None = None
    emergency_stop: bool | None = None
    enabled_topics: list[str] | None = None
    require_approved_kb: bool | None = None
    max_auto_replies_per_day: int | None = None
    max_auto_replies_per_conversation: int | None = None


class TakeoverIn(BaseModel):
    active: bool = True


@router.get("/kb/stats")
async def kb_stats(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    return await kb_service.stats(db)


@router.get("/kb/articles")
async def kb_list(
    db: Db,
    user: CurrentUser,
    status: str | None = None,
    category: str | None = None,
    language: str | None = None,
    q: str | None = None,
    needs_review: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    _ = user
    rows, total = await kb_service.list_articles(
        db,
        status=status,
        category=category,
        language=language,
        q=q,
        needs_review=needs_review,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "items": [kb_service.article_to_dict(r) for r in rows],
    }


@router.get("/kb/articles/{public_id}")
async def kb_get(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    row = await kb_service.get_by_public_id(db, public_id)
    if not row:
        raise HTTPException(404, "Article not found")
    data = kb_service.article_to_dict(row)
    data["source_preview"] = await kb_service.source_preview(db, row)
    if row.similar_to_id:
        sim = await db.get(type(row), row.similar_to_id)
        data["similar_article"] = kb_service.article_to_dict(sim) if sim else None
    if row.contradiction_of_id:
        contra = await db.get(type(row), row.contradiction_of_id)
        data["contradiction_article"] = kb_service.article_to_dict(contra) if contra else None
    return data


@router.post("/kb/articles")
async def kb_create(body: KbArticleIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    status = body.status or "approved"  # manual create → approved by default for admins
    row = await kb_service.create_article(db, user, body.model_dump(), status=status)
    await db.commit()
    return kb_service.article_to_dict(row)


@router.patch("/kb/articles/{public_id}")
async def kb_patch(public_id: str, body: KbArticleIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    row = await kb_service.get_by_public_id(db, public_id)
    if not row:
        raise HTTPException(404, "Article not found")
    data = body.model_dump(exclude_unset=True)
    status = data.pop("status", None)
    await kb_service.update_article(db, row, user, data, reason="admin_edit")
    if status:
        await kb_service.set_status(db, row, user, status, notes=data.get("review_notes") or "")
    await db.commit()
    return kb_service.article_to_dict(row)


@router.post("/kb/articles/{public_id}/review")
async def kb_review(public_id: str, body: KbStatusIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Approve / Edit+Approve / Reject / merge-flag via status."""
    _require_admin(user)
    row = await kb_service.get_by_public_id(db, public_id)
    if not row:
        raise HTTPException(404, "Article not found")
    edits = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
        if k in {"title", "question", "answer", "category", "product", "keywords", "language"} and v is not None
    }
    if edits:
        await kb_service.update_article(db, row, user, edits, reason="edit_before_review")
    await kb_service.set_status(db, row, user, body.status, notes=body.notes or "")
    await db.commit()
    return kb_service.article_to_dict(row)


@router.post("/kb/articles/{public_id}/merge")
async def kb_merge(
    public_id: str,
    db: Db,
    user: CurrentUser,
    into_id: str = Query(..., description="Target approved article public_id"),
) -> dict[str, Any]:
    _require_admin(user)
    row = await kb_service.get_by_public_id(db, public_id)
    target = await kb_service.get_by_public_id(db, into_id)
    if not row or not target:
        raise HTTPException(404, "Article not found")
    row.similar_to_id = target.id
    await kb_service.set_status(db, row, user, "archived", notes=f"Merged into {target.public_id}")
    # Optionally enrich target keywords
    if row.keywords and row.keywords not in (target.keywords or ""):
        target.keywords = ((target.keywords or "") + ", " + row.keywords).strip(", ")[:2000]
    await db.commit()
    return {"merged": row.public_id, "into": target.public_id, "target": kb_service.article_to_dict(target)}


@router.get("/kb/search")
async def kb_search(
    db: Db,
    user: CurrentUser,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=8, ge=1, le=20),
) -> dict[str, Any]:
    _ = user
    rows = await kb_service.search_approved(db, q, limit=limit)
    return {"items": [kb_service.article_to_dict(r) for r in rows]}


# ----- Learning -----


@router.post("/learning/run")
async def learning_run(body: LearnIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    result = await learning_mod.run_learning_batch(
        db,
        user=user,
        limit=body.limit,
        conversation_public_id=body.conversation_id,
        force=body.force,
    )
    await db.commit()
    return result


# ----- Corrections -----


@router.post("/corrections")
async def post_correction(body: CorrectionIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    try:
        row = await corrections_mod.record_correction(
            db,
            user,
            field_type=body.field_type,
            target_type=body.target_type,
            target_id=body.target_id,
            original_value=body.original_value,
            corrected_value=body.corrected_value,
            reason=body.reason,
            apply=body.apply,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return corrections_mod.correction_to_dict(row)


@router.get("/corrections")
async def get_corrections(
    db: Db,
    user: CurrentUser,
    field_type: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _ = user
    rows = await corrections_mod.list_corrections(
        db, field_type=field_type, target_type=target_type, target_id=target_id, limit=limit
    )
    return {"items": [corrections_mod.correction_to_dict(r) for r in rows]}


# ----- Reply assistant -----


async def _conv(db: AsyncSession, public_id: str) -> WhatsAppConversation:
    row = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.public_id == public_id,
                WhatsAppConversation.organization_id == "yesweigh",
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Conversation not found")
    return row


@router.post("/whatsapp/conversations/{conversation_id}/suggest-reply")
async def suggest_reply(
    conversation_id: str, body: SuggestIn, db: Db, user: CurrentUser
) -> dict[str, Any]:
    conv = await _conv(db, conversation_id)
    result = await reply_engine.suggest_reply(db, conv, user=user, force=body.force)
    await db.commit()
    return result


@router.get("/whatsapp/conversations/{conversation_id}/suggest-reply")
async def get_suggest_reply(conversation_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    conv = await _conv(db, conversation_id)
    row = await reply_engine.latest_pending_suggestion(db, conv.id)
    # Also return latest any status for panel
    if not row:
        row = (
            await db.execute(
                select(WaAiSuggestion)
                .where(WaAiSuggestion.conversation_id == conv.id)
                .order_by(WaAiSuggestion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    cfg = await reply_engine.get_or_create_config(db, conv.whatsapp_instance_id)
    return {
        "suggestion": reply_engine.suggestion_to_dict(row) if row else None,
        "human_takeover": bool(getattr(conv, "ai_human_takeover_at", None)),
        "config": reply_engine.config_to_dict(cfg),
        "auto_send_allowed": False,  # UI truth: never imply auto-send from this endpoint
    }


@router.patch("/whatsapp/suggestions/{suggestion_id}")
async def patch_suggestion(
    suggestion_id: str, body: SuggestionPatch, db: Db, user: CurrentUser
) -> dict[str, Any]:
    row = (
        await db.execute(select(WaAiSuggestion).where(WaAiSuggestion.public_id == suggestion_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Suggestion not found")
    if body.reply_text is not None:
        # Record correction when staff edits AI reply
        if (row.reply_text or "") != body.reply_text:
            try:
                await corrections_mod.record_correction(
                    db,
                    user,
                    field_type="suggested_reply",
                    target_type="suggestion",
                    target_id=row.public_id,
                    original_value=row.reply_text or "",
                    corrected_value=body.reply_text,
                    reason="inbox_edit",
                    apply=False,
                )
            except ValueError:
                pass
        row.reply_text = body.reply_text.strip()[:4000]
        if row.status == "pending":
            row.status = "edited"
    if body.feedback is not None:
        try:
            await reply_engine.set_suggestion_feedback(db, row, body.feedback)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.status is not None:
        st = body.status.strip().lower()
        if st not in {"pending", "edited", "sent", "discarded", "handover", "blocked"}:
            raise HTTPException(400, "Invalid status")
        row.status = st
    await db.commit()
    return reply_engine.suggestion_to_dict(row)


@router.post("/whatsapp/conversations/{conversation_id}/takeover")
async def takeover(conversation_id: str, body: TakeoverIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    conv = await _conv(db, conversation_id)
    if body.active:
        await reply_engine.mark_human_takeover(db, conv)
    else:
        await reply_engine.clear_human_takeover(db, conv)
    await db.commit()
    return {"human_takeover": bool(conv.ai_human_takeover_at), "conversation_id": conv.public_id}


# ----- Auto-reply config (admin) -----


@router.get("/auto-reply/configs")
async def list_auto_reply_configs(db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    instances = (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.organization_id == "yesweigh",
                WhatsAppInstance.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    items = []
    for inst in instances:
        cfg = await reply_engine.get_or_create_config(db, inst.id)
        items.append(
            {
                "instance": {
                    "id": inst.id,
                    "public_id": inst.public_id,
                    "display_name": inst.display_name,
                    "provider": inst.provider,
                    "connection_status": inst.connection_status,
                },
                "config": reply_engine.config_to_dict(cfg),
            }
        )
    await db.commit()
    return {"items": items, "default_mode": "suggest", "auto_send_default": False}


@router.patch("/auto-reply/configs/{instance_public_id}")
async def patch_auto_reply_config(
    instance_public_id: str, body: AutoReplyPatch, db: Db, user: CurrentUser
) -> dict[str, Any]:
    _require_admin(user)
    inst = (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.public_id == instance_public_id,
                WhatsAppInstance.organization_id == "yesweigh",
            )
        )
    ).scalar_one_or_none()
    if not inst:
        raise HTTPException(404, "Instance not found")
    cfg = await reply_engine.get_or_create_config(db, inst.id)
    try:
        await reply_engine.patch_config(db, cfg, user, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return {"instance_public_id": inst.public_id, "config": reply_engine.config_to_dict(cfg)}


# ----- Analytics -----


@router.get("/learning/analytics")
async def get_learning_analytics(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    return await reply_engine.learning_analytics(db)
