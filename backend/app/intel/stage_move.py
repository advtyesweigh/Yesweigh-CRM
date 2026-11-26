"""Persist manual Intelligence stage moves as training signals + KB learning."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.intel.enquiry_list import _insight_for
from app.intel.stage_kb import (
    extract_move_features,
    invalidate_stage_kb_cache,
    learn_from_move,
    load_recent_messages,
)
from app.intel.wa_stages import STAGE_KEYS
from app.models import AiStageMove, User, WhatsAppConversation

STAGE_LABELS = {
    "enquiry": "Enquiry",
    "sales_order": "Sales order",
    "waiting": "Waiting",
    "paid": "Paid",
    "stamping": "Stamping",
    "service": "Service",
    "commitments": "Commitments",
    "closed": "Closed",
}


def normalize_stage(raw: str) -> str:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "awaiting_payment": "waiting",
        "invoice_open": "waiting",
        "invoice_settled": "paid",
        "salesorder": "sales_order",
        "service_unresolved": "service",
        "dismiss": "closed",
        "dismissed": "closed",
    }
    key = aliases.get(key, key)
    if key not in STAGE_KEYS:
        raise ValueError(f"Unknown stage: {raw}")
    return key


async def move_conversation_stage(
    db: AsyncSession,
    *,
    user: User,
    conversation_public_id: str,
    to_stage: str,
    from_stage: str = "",
    extra_phrases: list[str] | None = None,
) -> dict[str, Any]:
    conv = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == "yesweigh",
                WhatsAppConversation.public_id == conversation_public_id,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise LookupError("Conversation not found")

    dest = normalize_stage(to_stage)
    src = ""
    if from_stage:
        try:
            src = normalize_stage(from_stage)
        except ValueError:
            src = (from_stage or "").strip().lower()

    insight = await _insight_for(db, int(conv.id))
    prev = (insight.stage_override or "").strip().lower()
    if not src and prev:
        src = prev

    now = datetime.now(timezone.utc)
    insight.stage_override = dest
    insight.stage_override_at = now
    insight.stage_override_by = (user.email or user.name or str(user.id))[:120]

    # Soft-close the conversation when dismissed from Intelligence.
    if dest == "closed" and (conv.status or "") != "Resolved":
        conv.status = "Resolved"

    # Extract recent chat features for training / KB.
    messages = await load_recent_messages(db, int(conv.id))
    features = extract_move_features(messages, to_stage=dest)
    if extra_phrases:
        from app.intel.stage_kb import _normalize_phrase

        for phrase in extra_phrases:
            cleaned = _normalize_phrase(phrase or "")
            if cleaned and cleaned not in features.phrases:
                features.phrases.insert(0, cleaned)
        features.phrases = features.phrases[:24]

    move = AiStageMove(
        conversation_id=int(conv.id),
        customer_jid=(conv.customer_jid or "")[:80],
        from_stage=src[:40],
        to_stage=dest[:40],
        actor_user_id=int(user.id) if user.id else None,
        actor_email=(user.email or "")[:255],
        snippet=features.snippet,
        signals_json=features.to_signals_json(),
    )
    db.add(move)
    await db.flush()

    learning = await learn_from_move(db, move=move, features=features, from_stage=src)
    invalidate_stage_kb_cache()
    try:
        from app.intel.cache import invalidate_intel_list_caches

        invalidate_intel_list_caches()
    except Exception:  # noqa: BLE001
        pass
    await db.commit()

    return {
        "conversation_id": conv.public_id,
        "customer_jid": conv.customer_jid or "",
        "from_stage": src,
        "to_stage": dest,
        "to_label": STAGE_LABELS.get(dest, dest),
        "stage_override": dest,
        "learning_updated": True,
        "learning": {
            "phrases": learning.get("phrases") or [],
            "kb_rows": learning.get("kb_rows") or 0,
        },
        "funnel_key": {
            "enquiry": "enquiry",
            "sales_order": "sales_order",
            "waiting": "invoice_open",
            "paid": "invoice_settled",
            "stamping": None,
            "service": None,
            "commitments": None,
            "closed": None,
        }.get(dest),
    }


async def move_stage_by_phone(
    db: AsyncSession,
    *,
    user: User,
    phone: str,
    to_stage: str,
    from_stage: str = "",
    extra_phrases: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve the best DM for a phone and move it (override + KB learn).

    Prefer the newest chat that is not blast-preview-only; still move all matching
    open DMs for that phone so duplicate threads stay in sync.
    """
    from app.ai.whatsapp_send_guards import phone_key
    from app.intel.dedupe import is_blast_summary
    from app.intel.wa_stages import dm_scope_clauses

    want = phone_key(phone)
    if not want:
        raise LookupError(f"Invalid phone: {phone}")
    rows = (
        await db.execute(
            select(WhatsAppConversation)
            .where(*dm_scope_clauses())
            .order_by(WhatsAppConversation.last_message_at.desc().nulls_last())
            .limit(800)
        )
    ).scalars().all()
    matches = []
    for conv in rows:
        if phone_key(conv.customer_phone or "") == want:
            matches.append(conv)
        elif phone_key((conv.customer_jid or "").split("@")[0]) == want:
            matches.append(conv)
    if not matches:
        raise LookupError(f"No conversation for phone {phone}")

    primary = next(
        (c for c in matches if not is_blast_summary(c.last_message_preview or "")),
        matches[0],
    )
    results = []
    for conv in matches:
        results.append(
            await move_conversation_stage(
                db,
                user=user,
                conversation_public_id=conv.public_id,
                to_stage=to_stage,
                from_stage=from_stage,
                extra_phrases=extra_phrases if conv.id == primary.id else None,
            )
        )
    primary_result = next(
        (r for r in results if r.get("conversation_id") == primary.public_id),
        results[0],
    )
    primary_result["moved_threads"] = len(results)
    return primary_result
