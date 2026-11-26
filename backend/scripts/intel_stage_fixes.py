"""One-shot Intelligence fixes: stage-move OV chats + close blast commitments.

Usage (inside CRM API container):
  python -m scripts.intel_stage_fixes
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from sqlalchemy import select, update

from app.ai.whatsapp_send_guards import phone_key
from app.db import SessionLocal
from app.intel.dedupe import is_blast_summary
from app.intel.stage_move import move_stage_by_phone
from app.models import AiCommitment, User, WhatsAppConversation


STAMPING_MOVES = (
    {
        "phone": "919567566831",
        "name": "Marvel Scales-Aluva",
        "extra_phrases": ["ov", "ov related", "stamping", "marvel scales"],
    },
    {
        "phone": "919947614194",
        "name": "Admali New",
        "extra_phrases": [
            "ov please send",
            "ov",
            "go26/26",
            "ayachilla",
            "അയച്ചില്ല",
            "yz01414",
        ],
    },
)

ROYAL_PHONE = "919895339647"


async def _actor(db) -> User:
    user = (
        await db.execute(select(User).where(User.email != "").order_by(User.id.asc()).limit(1))
    ).scalar_one_or_none()
    if user is not None:
        return user
    return SimpleNamespace(id=None, email="intel-stage-fixes@local", name="intel-stage-fixes")  # type: ignore[return-value]


async def close_blast_commitments(db) -> dict:
    rows = (
        await db.execute(
            select(AiCommitment).where(AiCommitment.status.in_(("open", "missed")))
        )
    ).scalars().all()
    ids = [int(r.id) for r in rows if is_blast_summary(r.promise_text or "")]
    if ids:
        await db.execute(
            update(AiCommitment).where(AiCommitment.id.in_(ids)).values(status="done")
        )
        await db.commit()
    return {"closed_blast_commitments": len(ids)}


async def count_royal_commitment_rows(db) -> int:
    want = phone_key(ROYAL_PHONE)
    conv_ids = []
    for conv in (
        await db.execute(select(WhatsAppConversation).where(WhatsAppConversation.organization_id == "yesweigh"))
    ).scalars().all():
        if phone_key(conv.customer_phone or "") == want:
            conv_ids.append(int(conv.id))
        elif phone_key((conv.customer_jid or "").split("@")[0]) == want:
            conv_ids.append(int(conv.id))
    if not conv_ids:
        return 0
    rows = (
        await db.execute(
            select(AiCommitment).where(
                AiCommitment.conversation_id.in_(conv_ids),
                AiCommitment.owner_side == "staff",
                AiCommitment.status.in_(("open", "missed")),
            )
        )
    ).scalars().all()
    # Mirror list API: drop blast, then unique conversation.
    kept = [r for r in rows if not is_blast_summary(r.promise_text or "")]
    return len({int(r.conversation_id) for r in kept})


async def main() -> int:
    async with SessionLocal() as db:
        actor = await _actor(db)
        before = await count_royal_commitment_rows(db)
        blast = await close_blast_commitments(db)
        results = []
        for spec in STAMPING_MOVES:
            try:
                out = await move_stage_by_phone(
                    db,
                    user=actor,  # type: ignore[arg-type]
                    phone=spec["phone"],
                    to_stage="stamping",
                    from_stage="commitments",
                    extra_phrases=list(spec["extra_phrases"]),
                )
                results.append({"name": spec["name"], "ok": True, **out})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": spec["name"], "ok": False, "error": str(exc)})
        after = await count_royal_commitment_rows(db)
        print(
            {
                "royal_open_commitment_convs_before_filter": before,
                "royal_open_commitment_convs_after": after,
                "blast": blast,
                "moves": results,
            }
        )
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
