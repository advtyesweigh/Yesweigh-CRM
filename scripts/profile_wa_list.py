"""Profile WhatsApp inbox list bottlenecks on VPS."""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.inbox import (
    _activity_at,
    _authorized_instance_ids,
    _lid_orphan_hidden_filter,
    _metrics,
    newsletter_list_filter,
)
from app.models import User, WhatsAppConversation
from app.whatsapp_chat_types import newsletter_list_filter as nl_filter


async def timed(label: str, coro):
    t = time.perf_counter()
    try:
        result = await coro
        print(f"{label}: {time.perf_counter() - t:.3f}s")
        return result
    except Exception as e:
        print(f"{label}: FAIL {time.perf_counter() - t:.3f}s {type(e).__name__}: {e}")
        raise


async def main():
    async with SessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(WhatsAppConversation))).scalar()
        print("conv_count", n)

        idx = (
            await db.execute(
                text(
                    """
                    SELECT indexname, indexdef FROM pg_indexes
                    WHERE tablename IN ('whatsapp_conversations', 'whatsapp_messages')
                    ORDER BY tablename, indexname
                    """
                )
            )
        ).fetchall()
        for name, defn in idx:
            print("IDX", name)
            print("   ", defn[:200])

        user = (
            await db.execute(select(User).where(User.email == "admin@yesweigh.in"))
        ).scalar_one()

        allowed = await timed("authorized_ids", _authorized_instance_ids(db, user))
        print("allowed", allowed)

        base = select(WhatsAppConversation).where(
            WhatsAppConversation.organization_id == "YesWeigh",
            nl_filter(),
        )
        activity = _activity_at()

        await timed(
            "list_no_lid_filter",
            db.execute(base.order_by(activity.desc().nullslast()).limit(5)),
        )

        base_lid = base.where(_lid_orphan_hidden_filter())
        await timed(
            "list_with_lid_filter",
            db.execute(base_lid.order_by(activity.desc().nullslast()).limit(5)),
        )

        # EXPLAIN ANALYZE lid filter
        explain = (
            await db.execute(
                text(
                    """
                    EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
                    SELECT c.id
                    FROM whatsapp_conversations c
                    WHERE c.organization_id = 'YesWeigh'
                      AND NOT (
                        c.customer_jid ILIKE '%@lid'
                        AND EXISTS (
                          SELECT 1 FROM whatsapp_conversations twin
                          WHERE twin.organization_id = c.organization_id
                            AND twin.whatsapp_instance_id = c.whatsapp_instance_id
                            AND twin.id <> c.id
                            AND twin.customer_lid = COALESCE(
                              NULLIF(TRIM(c.customer_lid), ''),
                              NULLIF(regexp_replace(split_part(c.customer_jid, '@', 1), ':.*$', ''), '')
                            )
                            AND COALESCE(NULLIF(TRIM(c.customer_lid), ''),
                              NULLIF(regexp_replace(split_part(c.customer_jid, '@', 1), ':.*$', ''), ''))
                                IS NOT NULL
                            AND COALESCE(NULLIF(TRIM(c.customer_lid), ''),
                              NULLIF(regexp_replace(split_part(c.customer_jid, '@', 1), ':.*$', ''), ''))
                                <> ''
                            AND (
                              twin.customer_jid ILIKE '%@s.whatsapp.net'
                              OR (
                                twin.customer_jid NOT ILIKE '%@lid'
                                AND length(regexp_replace(twin.customer_phone, '\\D', '', 'g'))
                                    BETWEEN 10 AND 15
                              )
                            )
                        )
                      )
                    ORDER BY COALESCE(c.last_message_at, c.updated_at, c.created_at) DESC NULLS LAST
                    LIMIT 5
                    """
                )
            )
        ).fetchall()
        print("--- EXPLAIN list+lid ---")
        for (line,) in explain:
            print(line)

        await timed("metrics_full", _metrics(db, allowed))

        # metrics without lid filter
        t = time.perf_counter()
        open_n = (
            await db.execute(
                select(func.count())
                .select_from(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == "YesWeigh",
                    WhatsAppConversation.status != "Resolved",
                    nl_filter(),
                )
            )
        ).scalar()
        print(f"metrics_open_simple: {time.perf_counter() - t:.3f}s open={open_n}")


if __name__ == "__main__":
    asyncio.run(main())
