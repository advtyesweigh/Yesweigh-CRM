"""Backfill whatsapp_reply_events from historical DM message pairs (batched)."""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import WhatsAppConversation, WhatsAppInstance, WhatsAppMessage, WhatsAppReplyEvent
from app.whatsapp_chat_types import dm_inbox_filter
from app.whatsapp_reply_latency import ORG, backfill_office_seconds_on_events, record_staff_reply_latency
from app.whatsapp_reply_needed import evaluate_inbound_reply_need

CONV_BATCH = 250
COMMIT_EVERY = 50


async def _existing_message_ids(db: AsyncSession) -> set[int]:
    rows = (await db.execute(select(WhatsAppReplyEvent.message_id).where(WhatsAppReplyEvent.message_id.isnot(None)))).all()
    return {int(r[0]) for r in rows if r[0] is not None}


async def process_conversation(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    inst_cache: dict[int, WhatsAppInstance | None],
    seen_msg: set[int],
) -> int:
    msgs = (
        (
            await db.execute(
                select(WhatsAppMessage)
                .where(
                    WhatsAppMessage.conversation_id == conv.id,
                    WhatsAppMessage.direction.in_(("in", "out")),
                )
                .order_by(WhatsAppMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) < 2:
        return 0
    iid = int(conv.whatsapp_instance_id)
    if iid not in inst_cache:
        inst_cache[iid] = await db.get(WhatsAppInstance, iid)
    inst = inst_cache[iid]
    created = 0
    awaiting_at = None
    recent: list[WhatsAppMessage] = []
    for m in msgs:
        if m.id and m.id in seen_msg:
            if m.direction == "out":
                awaiting_at = None
            continue
        recent = (recent + [m])[-12:]
        if m.direction == "in":
            body = (m.body or m.transcript or "").strip()
            if evaluate_inbound_reply_need(recent, body):
                if awaiting_at is None:
                    awaiting_at = m.created_at
            else:
                awaiting_at = None
        elif m.direction == "out" and awaiting_at is not None:
            evt = await record_staff_reply_latency(
                db,
                conv,
                awaiting_at=awaiting_at,
                replied_at=m.created_at,
                source="backfill",
                inst=inst,
                msg=m,
                skip_if_message_id_exists=False,
            )
            if evt and m.id:
                seen_msg.add(m.id)
                created += 1
            awaiting_at = None
    return created


async def backfill_reply_events(
    db: AsyncSession,
    *,
    full: bool = True,
    days: int | None = None,
    conv_limit: int | None = None,
) -> dict:
    seen_msg = await _existing_message_ids(db)
    inst_cache: dict[int, WhatsAppInstance | None] = {}
    scanned = 0
    created = 0
    last_id = 0
    pending_commit = 0

    while True:
        q = (
            select(WhatsAppConversation)
            .where(
                WhatsAppConversation.organization_id == ORG,
                dm_inbox_filter(),
                WhatsAppConversation.id > last_id,
            )
            .order_by(WhatsAppConversation.id.asc())
            .limit(CONV_BATCH)
        )
        if days is not None and not full:
            from datetime import datetime, timedelta, timezone

            since = datetime.now(timezone.utc) - timedelta(days=days)
            q = q.where(WhatsAppConversation.last_message_at >= since)
        convs = (await db.execute(q)).scalars().all()
        if not convs:
            break
        for conv in convs:
            last_id = conv.id
            scanned += 1
            n = await process_conversation(db, conv, inst_cache=inst_cache, seen_msg=seen_msg)
            created += n
            pending_commit += 1
            if pending_commit >= COMMIT_EVERY:
                await db.commit()
                pending_commit = 0
            if conv_limit and scanned >= conv_limit:
                await db.commit()
                return {"scanned_conversations": scanned, "events_created": created, "last_id": last_id}
        await db.commit()
        pending_commit = 0

    office_patched = await backfill_office_seconds_on_events(db)
    total_events = (await db.execute(select(func.count()).select_from(WhatsAppReplyEvent))).scalar() or 0
    return {
        "scanned_conversations": scanned,
        "events_created": created,
        "office_splits_patched": office_patched,
        "total_events": int(total_events),
        "last_id": last_id,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill WhatsApp staff reply latency events")
    parser.add_argument("--full", action="store_true", help="Scan all DM conversations (default)")
    parser.add_argument("--days", type=int, default=None, help="Limit to conversations active in last N days")
    parser.add_argument("--limit", type=int, default=None, help="Max conversations to scan")
    args = parser.parse_args()
    full = args.full or args.days is None
    async with SessionLocal() as db:
        result = await backfill_reply_events(db, full=full, days=args.days, conv_limit=args.limit)
        print(result)


if __name__ == "__main__":
    asyncio.run(main())