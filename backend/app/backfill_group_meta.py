"""Backfill WhatsApp group/channel subject + icon into whatsapp_conversations.

Usage (inside API container):
  PYTHONPATH=/app python -m app.backfill_group_meta
  PYTHONPATH=/app python -m app.backfill_group_meta --phone 120363423788627857 --force
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import or_, select

from app.crypto_util import decrypt_secret
from app.db import SessionLocal
from app.evolution import ORG, _svc
from app.models import WhatsAppConversation, WhatsAppInstance
from app.whatsapp_chat_meta import enrich_conversation_meta
from app.whatsapp_chat_types import is_group_jid, is_newsletter_jid, parse_group_rows

logger = logging.getLogger(__name__)


async def run(*, phone: str = "", force: bool = False) -> dict:
    stats = {
        "instances": 0,
        "candidates": 0,
        "named": 0,
        "photos": 0,
        "errors": [],
    }
    async with SessionLocal() as db:
        _, svc = await _svc(db)
        instances = (
            await db.execute(
                select(WhatsAppInstance).where(
                    WhatsAppInstance.organization_id == ORG,
                    WhatsAppInstance.deleted_at.is_(None),
                )
            )
        ).scalars().all()

        # Prefer connected instances first
        instances = sorted(
            instances,
            key=lambda i: 0 if "connect" in (i.connection_status or "").lower() else 1,
        )

        for inst in instances:
            stats["instances"] += 1
            token = decrypt_secret(inst.encrypted_instance_token)
            if not token:
                stats["errors"].append(f"inst {inst.id}: no token")
                continue

            name_by_jid: dict[str, str] = {}
            try:
                remote = await svc.list_groups(token)
                for row in parse_group_rows(remote):
                    if row.get("name"):
                        name_by_jid[row["jid"].lower()] = row["name"]
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"inst {inst.id} list: {exc}")

            q = select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                or_(
                    WhatsAppConversation.customer_jid.ilike("%@g.us"),
                    WhatsAppConversation.customer_jid.ilike("%@newsletter"),
                ),
            )
            if phone:
                digits = "".join(c for c in phone if c.isdigit())
                q = q.where(
                    or_(
                        WhatsAppConversation.customer_phone.contains(digits),
                        WhatsAppConversation.customer_jid.contains(digits),
                    )
                )
            rows = (await db.execute(q.order_by(WhatsAppConversation.id.desc()).limit(200))).scalars().all()
            for conv in rows:
                jid = (conv.customer_jid or "").strip()
                if not (is_group_jid(jid) or is_newsletter_jid(jid)):
                    continue
                stats["candidates"] += 1
                list_name = name_by_jid.get(jid.lower()) or ""
                if list_name and (force or not conv.customer_name or conv.customer_name == conv.customer_phone):
                    conv.customer_name = list_name[:160]
                    stats["named"] += 1
                before_name = (conv.customer_name or "").strip()
                before_photo = (conv.customer_photo or "").strip()
                try:
                    meta = await enrich_conversation_meta(
                        svc=svc, token=token, conv=conv, force=force
                    )
                    if (conv.customer_name or "").strip() and (conv.customer_name or "") != before_name:
                        stats["named"] += 1
                    if (conv.customer_photo or "").strip() and not before_photo:
                        stats["photos"] += 1
                    print(
                        f"id={conv.id} jid={jid} name={conv.customer_name!r} "
                        f"photo={'yes' if conv.customer_photo else 'no'} res={meta}"
                    )
                except Exception as exc:  # noqa: BLE001
                    stats["errors"].append(f"conv {conv.id}: {exc}")
            await db.commit()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--phone", default="")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    stats = asyncio.run(run(phone=args.phone, force=args.force))
    print(stats)


if __name__ == "__main__":
    main()
