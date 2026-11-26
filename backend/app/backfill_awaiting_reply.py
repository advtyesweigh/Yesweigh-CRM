"""Recompute awaiting_staff_reply + clear unread for ack / auto-reply / payment / staff-out DMs.

Rules in ``whatsapp_reply_needed`` apply globally to every agent/instance.
This backfill scans org-wide and prints before→after per instance.
Compatible with soft-spam hide and hide-groups (DM-only metrics).
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, or_, select

from app.db import SessionLocal
from app.models import WhatsAppConversation, WhatsAppInstance
from app.inbox import dm_inbox_filter, newsletter_list_filter
from app.whatsapp_chat_types import is_group_jid
from app.whatsapp_outline import load_recent_messages
from app.whatsapp_reply_needed import (
    is_customer_business_auto_reply,
    is_payment_confirmation,
    refresh_reply_state_from_messages,
)

ORG = "yesweigh"


def _not_spam_filter():
    """Match inbox unreplied badges — soft-spam threads stay hidden from counts."""
    return or_(
        WhatsAppConversation.is_spam.is_(False),
        WhatsAppConversation.is_spam.is_(None),
    )


async def _unreplied_for_instance(db, instance_id: int) -> int:
    n = (
        await db.execute(
            select(func.count())
            .select_from(WhatsAppConversation)
            .where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == instance_id,
                newsletter_list_filter(),
                dm_inbox_filter(),
                _not_spam_filter(),
                WhatsAppConversation.awaiting_staff_reply.is_(True),
                WhatsAppConversation.status != "Resolved",
            )
        )
    ).scalar_one()
    return int(n or 0)


async def _unread_sum_for_instance(db, instance_id: int) -> int:
    n = (
        await db.execute(
            select(func.coalesce(func.sum(WhatsAppConversation.unread_count), 0)).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == instance_id,
                newsletter_list_filter(),
                dm_inbox_filter(),
                _not_spam_filter(),
                WhatsAppConversation.status != "Resolved",
            )
        )
    ).scalar_one()
    return int(n or 0)


async def _unread_threads_for_instance(db, instance_id: int) -> int:
    n = (
        await db.execute(
            select(func.count())
            .select_from(WhatsAppConversation)
            .where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == instance_id,
                newsletter_list_filter(),
                dm_inbox_filter(),
                _not_spam_filter(),
                WhatsAppConversation.unread_count > 0,
                WhatsAppConversation.status != "Resolved",
            )
        )
    ).scalar_one()
    return int(n or 0)


async def _all_instances(db) -> list[WhatsAppInstance]:
    rows = (
        await db.execute(
            select(WhatsAppInstance)
            .where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
            )
            .order_by(WhatsAppInstance.display_name)
        )
    ).scalars().all()
    return list(rows)


async def main() -> None:
    stats: dict[str, Any] = {
        "scanned": 0,
        "updated": 0,
        "cleared_auto_reply": 0,
        "cleared_payment": 0,
        "cleared_ack_or_other": 0,
        "cleared_unread": 0,
    }
    # id -> (unreplied_b, unreplied_a, unread_sum_b, unread_sum_a, unread_threads_b, unread_threads_a, name)
    snap: dict[int, tuple[int, int, int, int, int, int, str]] = {}

    async with SessionLocal() as db:
        instances = await _all_instances(db)
        for inst in instances:
            snap[inst.id] = (
                await _unreplied_for_instance(db, inst.id),
                -1,
                await _unread_sum_for_instance(db, inst.id),
                -1,
                await _unread_threads_for_instance(db, inst.id),
                -1,
                inst.display_name or "",
            )

        awaiting_rows = (
            await db.execute(
                select(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.awaiting_staff_reply.is_(True),
                    ~WhatsAppConversation.customer_jid.ilike("%@g.us"),
                    WhatsAppConversation.status != "Resolved",
                )
            )
        ).scalars().all()
        unread_rows = (
            await db.execute(
                select(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.unread_count > 0,
                    ~WhatsAppConversation.customer_jid.ilike("%@g.us"),
                    WhatsAppConversation.status != "Resolved",
                )
            )
        ).scalars().all()
        recent_rows = (
            await db.execute(
                select(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    ~WhatsAppConversation.customer_jid.ilike("%@g.us"),
                    WhatsAppConversation.status != "Resolved",
                    or_(
                        WhatsAppConversation.awaiting_staff_reply.is_(True),
                        WhatsAppConversation.unread_count > 0,
                    ),
                )
                .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
                .limit(8000)
            )
        ).scalars().all()
        recent_all = (
            await db.execute(
                select(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    ~WhatsAppConversation.customer_jid.ilike("%@g.us"),
                    WhatsAppConversation.status != "Resolved",
                )
                .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
                .limit(5000)
            )
        ).scalars().all()
        by_id: dict[int, WhatsAppConversation] = {}
        for conv in (*awaiting_rows, *unread_rows, *recent_rows, *recent_all):
            by_id[conv.id] = conv
        rows = list(by_id.values())

        for conv in rows:
            stats["scanned"] += 1
            if is_group_jid(conv.customer_jid or ""):
                if conv.awaiting_staff_reply or (conv.unread_count or 0) > 0:
                    conv.awaiting_staff_reply = False
                    if (conv.unread_count or 0) > 0:
                        conv.unread_count = 0
                        stats["cleared_unread"] += 1
                    stats["updated"] += 1
                continue
            msgs = await load_recent_messages(db, conv, limit=16)
            last_in_body = ""
            for m in reversed(msgs):
                if m.direction == "in":
                    last_in_body = (m.body or m.transcript or "").strip()
                    break
            before_await = bool(conv.awaiting_staff_reply)
            before_unread = int(conv.unread_count or 0)
            after = refresh_reply_state_from_messages(conv, msgs)
            after_unread = int(conv.unread_count or 0)
            if before_unread > 0 and after_unread == 0:
                stats["cleared_unread"] += 1
            if before_await and not after:
                stats["updated"] += 1
                if is_payment_confirmation(last_in_body):
                    stats["cleared_payment"] += 1
                elif is_customer_business_auto_reply(last_in_body):
                    stats["cleared_auto_reply"] += 1
                else:
                    stats["cleared_ack_or_other"] += 1
            elif before_await != after or before_unread != after_unread:
                stats["updated"] += 1

        for inst in instances:
            ub, _, usb, _, utb, _, name = snap[inst.id]
            snap[inst.id] = (
                ub,
                await _unreplied_for_instance(db, inst.id),
                usb,
                await _unread_sum_for_instance(db, inst.id),
                utb,
                await _unread_threads_for_instance(db, inst.id),
                name,
            )

        await db.commit()

    print(
        "backfill_awaiting_reply "
        f"scanned={stats['scanned']} updated={stats['updated']} "
        f"cleared_auto_reply={stats['cleared_auto_reply']} "
        f"cleared_payment={stats['cleared_payment']} "
        f"cleared_ack_or_other={stats['cleared_ack_or_other']} "
        f"cleared_unread={stats['cleared_unread']}"
    )
    for iid, (ub, ua, usb, usa, utb, uta, name) in sorted(snap.items(), key=lambda x: (x[1][6] or "").lower()):
        print(
            f"agent id={iid} name={name!r} "
            f"unreplied {ub}->{ua} "
            f"unread_sum {usb}->{usa} "
            f"unread_threads {utb}->{uta}"
        )


if __name__ == "__main__":
    asyncio.run(main())
