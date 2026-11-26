"""One-shot Intelligence fixes: stage-move OV chats + close blast commitments.

  python -m app.intel.stage_fixes
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


_JUNK_KB = (
    "document",
    "photo",
    "video",
    "voice message",
    "📄 document",
    "📷 photo",
    "🎥 video",
    "🎤 voice message",
    "media",
    "sticker",
    "attachment",
    "ov",  # too short / too broad alone — keep "ov please send" etc.
)


async def scrub_junk_kb(db) -> dict:
    from sqlalchemy import delete

    from app.intel.stage_kb import invalidate_stage_kb_cache
    from app.models import AiStageKb

    rows = (
        await db.execute(select(AiStageKb).where(AiStageKb.stage == "stamping"))
    ).scalars().all()
    drop_ids: list[int] = []
    for row in rows:
        phrase = (row.phrase or "").strip().lower()
        if phrase in _JUNK_KB:
            drop_ids.append(int(row.id))
            continue
        if phrase.startswith(("📄", "📷", "🎥", "🎤")):
            drop_ids.append(int(row.id))
            continue
        # Generic voice fillers without OV evidence.
        if phrase.startswith("voice message") and not any(
            tok in phrase for tok in ("ov", "stamp", "ഓവി", "ഒവി", "ayachilla", "അയച്ചില്ല")
        ):
            drop_ids.append(int(row.id))
    if drop_ids:
        await db.execute(delete(AiStageKb).where(AiStageKb.id.in_(drop_ids)))
        await db.commit()
        invalidate_stage_kb_cache()
    return {"scrubbed_kb_rows": len(drop_ids)}


async def count_royal_open_commitments(db) -> dict:
    want = phone_key(ROYAL_PHONE)
    conv_ids: list[int] = []
    for conv in (
        await db.execute(
            select(WhatsAppConversation).where(WhatsAppConversation.organization_id == "yesweigh")
        )
    ).scalars().all():
        if phone_key(conv.customer_phone or "") == want:
            conv_ids.append(int(conv.id))
        elif phone_key((conv.customer_jid or "").split("@")[0]) == want:
            conv_ids.append(int(conv.id))
    if not conv_ids:
        return {"raw_rows": 0, "after_blast_filter": 0, "after_dedupe": 0, "conv_ids": []}
    rows = (
        await db.execute(
            select(AiCommitment).where(
                AiCommitment.conversation_id.in_(conv_ids),
                AiCommitment.owner_side == "staff",
                AiCommitment.status.in_(("open", "missed")),
            )
        )
    ).scalars().all()
    kept = [r for r in rows if not is_blast_summary(r.promise_text or "")]
    return {
        "raw_rows": len(rows),
        "after_blast_filter": len(kept),
        "after_dedupe": len({int(r.conversation_id) for r in kept}),
        "conv_ids": sorted(set(conv_ids)),
    }


async def main() -> int:
    async with SessionLocal() as db:
        actor = await _actor(db)
        royal_before = await count_royal_open_commitments(db)
        blast = await close_blast_commitments(db)
        scrub = await scrub_junk_kb(db)
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
        royal_after = await count_royal_open_commitments(db)

        # Confirm stamping overrides / queue membership for Marvel + Admali.
        from app.intel.queries import stamping_queue

        stamp_items = await stamping_queue(db, allowed_instance_ids=None, limit=300)
        stamp_phones = {phone_key(str(i.get("phone") or "")) for i in stamp_items}
        print(
            {
                "royal_before": royal_before,
                "royal_after": royal_after,
                "blast": blast,
                "scrub": scrub,
                "moves": results,
                "marvel_on_stamping": phone_key("919567566831") in stamp_phones,
                "admali_on_stamping": phone_key("919947614194") in stamp_phones,
                "stamping_count": len(stamp_items),
            }
        )
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
