"""Repair group chats misfiled under participant LID/PN DMs.

Evolution group MESSAGE webhooks use Chat=@g.us + SenderAlt=@lid. A prior bug
rewrote the conversation jid to the participant LID, so inbox showed the person
name and raw @LID mentions instead of the group subject.

Usage (API container):
  PYTHONPATH=/app python -m app.repair_group_inbox
  PYTHONPATH=/app python -m app.repair_group_inbox --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.crypto_util import decrypt_secret
from app.db import SessionLocal
from app.evolution import (
    ORG,
    _extract_message_fields,
    _svc,
    humanize_lid_mentions,
)
from app.models import EvolutionWebhookEvent, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage
from app.whatsapp_chat_meta import enrich_conversation_meta
from app.whatsapp_chat_types import is_group_jid

logger = logging.getLogger(__name__)


def _event_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def _ensure_group_conv(
    db,
    *,
    inst: WhatsAppInstance,
    group_jid: str,
    group_phone: str,
) -> WhatsAppConversation:
    existing = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                WhatsAppConversation.customer_jid == group_jid,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    # Also match by phone digits when jid was never written correctly
    by_phone = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                WhatsAppConversation.customer_phone == group_phone,
                WhatsAppConversation.customer_jid.ilike("%@g.us"),
            )
        )
    ).scalar_one_or_none()
    if by_phone:
        by_phone.customer_jid = group_jid
        return by_phone
    import uuid

    conv = WhatsAppConversation(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        whatsapp_instance_id=inst.id,
        customer_phone=group_phone,
        customer_name="",
        customer_jid=group_jid,
        customer_lid="",
        status="Open",
        tag="",
        last_message_preview="",
        last_message_at=None,
        unread_count=0,
    )
    db.add(conv)
    await db.flush()
    return conv


async def run(*, limit: int = 300) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "events": 0,
        "groups": 0,
        "moved": 0,
        "named": 0,
        "previews": 0,
        "errors": [],
    }
    touched: dict[int, WhatsAppConversation] = {}

    async with SessionLocal() as db:
        _, svc = await _svc(db)
        rows = (
            await db.execute(
                select(EvolutionWebhookEvent)
                .where(
                    EvolutionWebhookEvent.event_type.in_(["Message", "message", "messages.upsert"]),
                    EvolutionWebhookEvent.payload.ilike("%@g.us%"),
                )
                .order_by(EvolutionWebhookEvent.id.desc())
                .limit(limit)
            )
        ).scalars().all()

        inst_cache: dict[int, WhatsAppInstance | None] = {}

        for ev in rows:
            stats["events"] += 1
            payload = _event_payload(ev.payload)
            fields = _extract_message_fields(payload)
            if not fields or not fields.get("is_group"):
                continue
            jid = (fields.get("jid") or "").strip()
            if not is_group_jid(jid):
                continue
            inst_id = ev.whatsapp_instance_id
            if inst_id not in inst_cache:
                inst_cache[inst_id] = await db.get(WhatsAppInstance, inst_id)
            inst = inst_cache[inst_id]
            if not inst or inst.deleted_at:
                continue

            phone = "".join(c for c in (fields.get("phone") or "") if c.isdigit())
            try:
                conv = await _ensure_group_conv(db, inst=inst, group_jid=jid, group_phone=phone)
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"ensure {jid}: {exc}")
                continue
            touched[conv.id] = conv
            stats["groups"] = len({c.customer_jid for c in touched.values()})

            ext = (fields.get("external_id") or "").strip()
            if not ext:
                continue
            msg = (
                await db.execute(
                    select(WhatsAppMessage).where(
                        WhatsAppMessage.whatsapp_instance_id == inst.id,
                        WhatsAppMessage.external_message_id == ext,
                    )
                )
            ).scalar_one_or_none()
            if not msg:
                continue

            body = await humanize_lid_mentions(db, instance_id=inst.id, text=msg.body or "")
            if body != (msg.body or ""):
                msg.body = body
            sender = (
                "WhatsApp"
                if msg.direction == "out"
                else (fields.get("push_name") or msg.sender_name or "")
            )
            if sender and msg.sender_name != sender:
                msg.sender_name = sender[:120]

            if msg.conversation_id != conv.id:
                msg.conversation_id = conv.id
                stats["moved"] += 1

            preview = body
            if msg.direction == "in" and (fields.get("push_name") or "").strip():
                preview = f"{fields['push_name'].strip()}: {body}"
            event_at = fields.get("timestamp") or msg.created_at
            last_at = conv.last_message_at
            if last_at is not None and last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            if event_at and (last_at is None or event_at >= last_at):
                conv.last_message_preview = preview[:255]
                conv.last_message_at = event_at if isinstance(event_at, datetime) else last_at
                stats["previews"] += 1

        # Enrich group subject/icon for touched rows
        for conv in touched.values():
            inst = inst_cache.get(conv.whatsapp_instance_id) or await db.get(
                WhatsAppInstance, conv.whatsapp_instance_id
            )
            if not inst:
                continue
            token = decrypt_secret(inst.encrypted_instance_token)
            if not token:
                continue
            before = (conv.customer_name or "").strip()
            try:
                meta = await enrich_conversation_meta(svc=svc, token=token, conv=conv, force=True)
                if (conv.customer_name or "").strip() and (conv.customer_name or "") != before:
                    stats["named"] += 1
                print(
                    f"group id={conv.id} jid={conv.customer_jid} name={conv.customer_name!r} "
                    f"preview={(conv.last_message_preview or '')[:80]!r} meta={meta.get('reason')}"
                )
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"meta {conv.id}: {exc}")

        await db.commit()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=300)
    args = p.parse_args()
    stats = asyncio.run(run(limit=args.limit))
    print(stats)


if __name__ == "__main__":
    main()
