"""Employee corrections — store original + corrected for prompt/retrieval/eval (not fine-tuning)."""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiCorrection, Employee, User, WhatsAppConversation, WhatsAppMessage

ORG = "yesweigh"
FIELD_TYPES = frozenset(
    {"transcript", "translation", "summary", "suggested_reply", "kb_suggestion"}
)
TARGET_TYPES = frozenset({"message", "conversation", "kb_article", "suggestion"})


def correction_to_dict(row: AiCorrection) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    try:
        meta = json.loads(row.meta_json or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except Exception:  # noqa: BLE001
        meta = {}
    return {
        "id": row.public_id,
        "field_type": row.field_type,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "original_value": row.original_value or "",
        "corrected_value": row.corrected_value or "",
        "reason": row.reason or "",
        "employee_id": row.employee_id,
        "user_id": row.user_id,
        "locked": bool(row.locked),
        "meta": meta,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def record_correction(
    db: AsyncSession,
    user: User,
    *,
    field_type: str,
    target_type: str,
    target_id: str,
    original_value: str,
    corrected_value: str,
    reason: str = "",
    apply: bool = True,
    meta: dict[str, Any] | None = None,
) -> AiCorrection:
    ft = (field_type or "").strip().lower()
    tt = (target_type or "").strip().lower()
    if ft not in FIELD_TYPES:
        raise ValueError(f"Invalid field_type: {field_type}")
    if tt not in TARGET_TYPES:
        raise ValueError(f"Invalid target_type: {target_type}")
    if not (target_id or "").strip():
        raise ValueError("target_id required")
    if (corrected_value or "").strip() == (original_value or "").strip():
        raise ValueError("corrected_value must differ from original")

    emp = (
        await db.execute(select(Employee).where(Employee.user_id == user.id))
    ).scalar_one_or_none()

    row = AiCorrection(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        field_type=ft,
        target_type=tt,
        target_id=str(target_id).strip(),
        original_value=(original_value or "")[:20000],
        corrected_value=(corrected_value or "")[:20000],
        reason=(reason or "").strip()[:2000],
        employee_id=emp.id if emp else None,
        user_id=user.id,
        locked=True,
        meta_json=json.dumps(meta or {}),
    )
    db.add(row)
    await db.flush()

    if apply:
        await _apply_correction(db, row)
    return row


async def _apply_correction(db: AsyncSession, row: AiCorrection) -> None:
    """Apply correction to live CRM fields without destroying history (locked=True)."""
    if row.field_type in {"transcript", "translation"} and row.target_type == "message":
        msg = (
            await db.execute(
                select(WhatsAppMessage).where(WhatsAppMessage.public_id == row.target_id)
            )
        ).scalar_one_or_none()
        if not msg:
            return
        # Never blank a valid transcript with empty correction
        new_v = (row.corrected_value or "").strip()
        if not new_v:
            return
        msg.transcript = new_v
        if row.field_type == "translation" and not (msg.transcript_language or "").strip():
            msg.transcript_language = "ml"
        await db.flush()
        return

    if row.field_type == "summary" and row.target_type == "conversation":
        conv = (
            await db.execute(
                select(WhatsAppConversation).where(WhatsAppConversation.public_id == row.target_id)
            )
        ).scalar_one_or_none()
        if not conv:
            return
        new_v = (row.corrected_value or "").strip()
        if not new_v:
            return
        conv.ai_outline = new_v
        await db.flush()
        return


async def list_corrections(
    db: AsyncSession,
    *,
    field_type: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = 50,
) -> list[AiCorrection]:
    lim = max(1, min(int(limit or 50), 200))
    q = select(AiCorrection).where(AiCorrection.organization_id == ORG)
    if field_type:
        q = q.where(AiCorrection.field_type == field_type.strip().lower())
    if target_type:
        q = q.where(AiCorrection.target_type == target_type.strip().lower())
    if target_id:
        q = q.where(AiCorrection.target_id == str(target_id).strip())
    q = q.order_by(AiCorrection.id.desc()).limit(lim)
    return list((await db.execute(q)).scalars().all())


async def terminology_hints(db: AsyncSession, *, limit: int = 40) -> list[dict[str, str]]:
    """Recent locked corrections useful for reply prompts (short originals → preferred wording)."""
    rows = await list_corrections(db, limit=limit)
    hints: list[dict[str, str]] = []
    for r in rows:
        if not r.locked:
            continue
        o = (r.original_value or "").strip()
        c = (r.corrected_value or "").strip()
        if not c or len(c) > 400:
            continue
        hints.append(
            {
                "field_type": r.field_type,
                "prefer": c[:400],
                "instead_of": o[:200] if len(o) < 200 else "",
            }
        )
    return hints[:limit]
