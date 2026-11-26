"""Backfill whatsapp_conversations.service_category + priority from cached outlines and heuristics."""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.models import WhatsAppConversation
from app.whatsapp_service_meta import (
    apply_service_meta_to_conversation,
    parse_cached_outline_summary,
)


async def run(*, limit: int = 5000, only_open: bool = True) -> dict[str, int]:
    updated = 0
    outline_patched = 0
    scanned = 0
    async with SessionLocal() as db:
        stmt = select(WhatsAppConversation).order_by(WhatsAppConversation.id.desc()).limit(limit)
        if only_open:
            stmt = stmt.where(WhatsAppConversation.status != "Resolved")
        rows = (await db.execute(stmt)).scalars().all()
        for conv in rows:
            scanned += 1
            raw_outline = getattr(conv, "ai_outline", None) or ""
            summary = parse_cached_outline_summary(raw_outline)
            before = (conv.service_category or "", conv.priority or "")
            apply_service_meta_to_conversation(
                conv,
                summary,
                fallback_text=conv.last_message_preview or "",
            )
            after = (conv.service_category or "", conv.priority or "")
            if after != before:
                updated += 1
            if raw_outline.strip().startswith("{"):
                new_outline = json.dumps(summary, ensure_ascii=False)
                if new_outline != raw_outline.strip():
                    conv.ai_outline = new_outline
                    outline_patched += 1
        legacy = await db.execute(
            update(WhatsAppConversation)
            .where(func.lower(WhatsAppConversation.service_category) == "support")
            .values(service_category="sales")
        )
        await db.commit()
    return {
        "scanned": scanned,
        "updated": updated,
        "outline_patched": outline_patched,
        "legacy_support_rows": legacy.rowcount or 0,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill WhatsApp service_category from AI outline + keywords")
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--all-statuses", action="store_true", help="Include resolved conversations")
    args = p.parse_args()
    result = asyncio.run(run(limit=args.limit, only_open=not args.all_statuses))
    print(result)


if __name__ == "__main__":
    main()