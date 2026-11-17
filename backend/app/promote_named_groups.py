"""Ensure named @g.us conversations surface in the inbox list.

Repaired / backfilled groups sometimes have a subject but null last_message_at,
so ORDER BY last_message_at NULLS LAST pushes them past the FE limit (80).

Usage (API container):
  PYTHONPATH=/app python -m app.promote_named_groups
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.evolution import ORG
from app.models import WhatsAppConversation
from app.whatsapp_chat_types import is_group_jid


def _is_raw_id_name(name: str, phone: str, jid: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if phone and n == phone.strip():
        return True
    jid_user = (jid or "").split("@", 1)[0]
    if jid_user and n == jid_user:
        return True
    digits = re.sub(r"\D+", "", n.lstrip("@"))
    if n.startswith("@") and len(digits) >= 14 and digits == n.lstrip("@"):
        return True
    if n.isdigit() and len(n) >= 14:
        return True
    return False


async def run() -> dict[str, Any]:
    stats: dict[str, Any] = {"touched": 0, "names_fixed": 0, "groups": []}
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(WhatsAppConversation).where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.customer_jid.ilike("%@g.us"),
                )
            )
        ).scalars().all()
        for conv in rows:
            jid = conv.customer_jid or ""
            if not is_group_jid(jid):
                continue
            name = (conv.customer_name or "").strip()
            # Collapse accidental double spaces from Evolution subjects
            cleaned = re.sub(r"\s{2,}", " ", name).strip()
            if cleaned and cleaned != name:
                conv.customer_name = cleaned[:160]
                name = cleaned
                stats["names_fixed"] += 1
            if _is_raw_id_name(name, conv.customer_phone or "", jid):
                continue
            changed = False
            if conv.last_message_at is None:
                conv.last_message_at = conv.updated_at or conv.created_at
                changed = True
            preview = (conv.last_message_preview or "").strip()
            if not preview:
                conv.last_message_preview = "WhatsApp group"
                changed = True
            if changed:
                stats["touched"] += 1
                stats["groups"].append(
                    {
                        "id": conv.id,
                        "name": name,
                        "jid": jid,
                        "instance": conv.whatsapp_instance_id,
                        "last_message_at": str(conv.last_message_at),
                    }
                )
        await db.commit()
    return stats


def main() -> None:
    print(asyncio.run(run()))


if __name__ == "__main__":
    main()
