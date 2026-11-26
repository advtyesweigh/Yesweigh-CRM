"""Rewrite cached outlines that are still labelled as sales enquiries but are not.

Rules only. Does not call Gemini and does not change awaiting_staff_reply.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import or_, select

from app.db import SessionLocal
from app.intel.enquiry_list import (
    _insight_for,
    message_windows,
    rewrite_outline_if_needed,
    staff_directory,
    store_cache,
    verdict_for,
)
from app.models import WhatsAppConversation
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter


async def backfill_enquiry_labels(limit: int = 2500, *, rescore: bool = True) -> dict[str, int]:
    async with SessionLocal() as db:
        phones, first = await staff_directory(db)
        rows = (
            await db.execute(
                select(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == "yesweigh",
                    newsletter_list_filter(),
                    dm_inbox_filter(),
                    or_(
                        WhatsAppConversation.ai_outline.ilike("%New Enquiry%"),
                        WhatsAppConversation.ai_outline.ilike("%Interested%"),
                        WhatsAppConversation.service_category.in_(["sales", "support", "Sales"]),
                    ),
                )
                .order_by(WhatsAppConversation.last_message_at.desc().nulls_last())
                .limit(limit)
            )
        ).scalars().all()
        windows = await message_windows(db, [int(c.id) for c in rows])
        rewritten = 0
        cached = 0
        for conv in rows:
            verdict = await verdict_for(
                db,
                conv,
                windows.get(int(conv.id), []),
                phones=phones,
                first=first,
                allow_gemini=False,
            )
            insight = await _insight_for(db, int(conv.id))
            store_cache(insight, conv, verdict)
            cached += 1
            if rewrite_outline_if_needed(conv, verdict):
                rewritten += 1
        if rewritten or cached:
            await db.commit()
        else:
            await db.rollback()
        print(f"enquiry_backfill scanned={len(rows)} cached={cached} rewritten={rewritten}")
        return {"scanned": len(rows), "cached": cached, "rewritten": rewritten}


def main() -> None:
    asyncio.run(backfill_enquiry_labels())


if __name__ == "__main__":
    main()
