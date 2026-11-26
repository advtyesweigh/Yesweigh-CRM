"""Backfill CRM Leads + sales attendance fields for dealer-enquiry WhatsApp chats."""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from app.db import SessionLocal
from app.models import WhatsAppConversation, WhatsAppInstance, WhatsAppMessage
from app.whatsapp_dealer_enquiry import (
    apply_dealer_enquiry_actions,
    dealer_enquiry_text_signal,
    is_dealer_enquiry_summary,
)
from app.whatsapp_outline import load_recent_messages
from app.whatsapp_service_meta import parse_cached_outline_summary

PROVIDER_META_CLOUD = "meta_cloud"


async def run(
    *,
    limit: int = 800,
    only_care: bool = True,
    force_all: bool = False,
) -> dict[str, int]:
    leads_created = 0
    outlines_patched = 0
    scanned = 0
    async with SessionLocal() as db:
        care_ids: list[int] = []
        if only_care:
            care_ids = list(
                (
                    await db.execute(
                        select(WhatsAppInstance.id).where(
                            WhatsAppInstance.provider == PROVIDER_META_CLOUD,
                            WhatsAppInstance.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
            )
        stmt = select(WhatsAppConversation).order_by(WhatsAppConversation.last_message_at.desc().nullslast())
        if only_care and care_ids:
            stmt = stmt.where(WhatsAppConversation.whatsapp_instance_id.in_(care_ids))
        stmt = stmt.limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
        for conv in rows:
            scanned += 1
            raw = (conv.ai_outline or "").strip()
            summary = parse_cached_outline_summary(raw) if raw else {}
            preview = conv.last_message_preview or ""
            if not force_all and not is_dealer_enquiry_summary(summary, fallback_text=preview):
                if not dealer_enquiry_text_signal(preview):
                    continue
            messages = await load_recent_messages(db, conv, limit=60)
            before_phone_lead = await _lead_count_phone(db, conv.customer_phone)
            new_summary = await apply_dealer_enquiry_actions(db, conv, summary, messages)
            after_phone_lead = await _lead_count_phone(db, conv.customer_phone)
            if after_phone_lead > before_phone_lead:
                leads_created += 1
            encoded = json.dumps(new_summary, ensure_ascii=False)
            if encoded != raw:
                conv.ai_outline = encoded
                outlines_patched += 1
        await db.commit()
    return {
        "scanned": scanned,
        "leads_created": leads_created,
        "outlines_patched": outlines_patched,
    }


async def _lead_count_phone(db, phone: str) -> int:
    from app.inbox import _find_lead_by_phone

    lead = await _find_lead_by_phone(db, phone)
    return 1 if lead else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill dealer-enquiry leads + sales attendance on outlines")
    p.add_argument("--limit", type=int, default=800)
    p.add_argument("--all-instances", action="store_true", help="Not only Care meta_cloud")
    p.add_argument("--force-signal", action="store_true", help="Re-run on any dealer-enquiry heuristic match")
    args = p.parse_args()
    result = asyncio.run(
        run(limit=args.limit, only_care=not args.all_instances, force_all=args.force_signal)
    )
    print(result)


if __name__ == "__main__":
    main()