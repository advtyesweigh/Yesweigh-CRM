"""Repair Evolution messages stored as bare 'Attachment' / missing media.

Re-parses stored webhook payloads with current extractors, updates body text,
and retries media persist (download + JPEGThumbnail fallback).
"""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import or_, select

from app.crypto_util import decrypt_secret
from app.db import SessionLocal, engine
from app.evolution import _extract_message_fields, _persist_message_media, _svc
from app.models import EvolutionWebhookEvent, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage

PLACEHOLDER_BODIES = {
    "",
    "[media]",
    "media",
    "Attachment",
    "📷 Photo",
    "🎥 Video",
    "🎤 Voice message",
    "📄 Document",
    "Sticker",
    "Unsupported message",
}


async def main() -> None:
    async with SessionLocal() as db:
        targets = (
            await db.execute(
                select(WhatsAppMessage)
                .where(
                    or_(
                        WhatsAppMessage.body.in_(list(PLACEHOLDER_BODIES)),
                        WhatsAppMessage.body == "Attachment",
                        (WhatsAppMessage.media_type != "")
                        & or_(WhatsAppMessage.media_url == "", WhatsAppMessage.media_url.is_(None)),
                    )
                )
                .order_by(WhatsAppMessage.id.desc())
                .limit(200)
            )
        ).scalars().all()
        print(f"candidate messages: {len(targets)}")
        _, svc = await _svc(db)
        updated = 0
        for msg in targets:
            ext = (msg.external_message_id or "").strip()
            if not ext:
                continue
            # Exact match on Evolution Info.ID — LIKE '%id%' can hit quoted stubs.
            from sqlalchemy import text as sql_text

            row = (
                await db.execute(
                    sql_text(
                        """
                        SELECT id, payload FROM evolution_webhook_events
                        WHERE payload LIKE :needle
                          AND payload::text LIKE :info_id
                        ORDER BY id DESC
                        LIMIT 5
                        """
                    ),
                    {"needle": f"%{ext}%", "info_id": f'%\"ID\": \"{ext}\"%'},
                )
            ).mappings().first()
            if not row:
                print(f"skip {msg.id} {ext}: no webhook")
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
                info = ((payload.get("data") or {}).get("Info") or {})
                if str(info.get("ID") or "") != ext:
                    print(f"skip {msg.id} {ext}: Info.ID mismatch")
                    continue
            except json.JSONDecodeError:
                print(f"skip {msg.id}: bad json")
                continue
            fields = _extract_message_fields(payload)
            if not fields:
                print(f"skip {msg.id}: extract empty")
                continue

            new_body = (fields.get("text") or "").strip()
            new_media_type = (fields.get("media_type") or "").strip()
            saved: dict[str, str] = {}
            if new_media_type or msg.media_type:
                inst = await db.get(WhatsAppInstance, msg.whatsapp_instance_id)
                if inst:
                    token = decrypt_secret(inst.encrypted_instance_token)
                    try:
                        saved = await _persist_message_media(svc, token, payload)
                    except Exception as exc:  # noqa: BLE001
                        print(f"media fail {ext}: {exc}")
                        saved = {}

            changed = False
            if saved.get("media_url"):
                msg.media_url = saved["media_url"]
                msg.media_type = saved.get("media_type") or new_media_type or msg.media_type
                msg.media_name = saved.get("media_name") or msg.media_name
                changed = True
            elif new_media_type and not msg.media_type:
                msg.media_type = new_media_type
                changed = True

            if new_body and new_body != "Attachment" and (
                (msg.body or "").strip() in PLACEHOLDER_BODIES or (msg.body or "").strip() != new_body
            ):
                # Prefer extracted text over placeholder; keep media label only if still empty.
                if new_body not in PLACEHOLDER_BODIES or (msg.body or "").strip() in {
                    "Attachment",
                    "[media]",
                    "media",
                    "",
                }:
                    msg.body = new_body
                    changed = True
            elif saved.get("media_type") and (msg.body or "").strip() in {
                "Attachment",
                "[media]",
                "media",
                "",
                "Unsupported message",
            }:
                labels = {
                    "image": "📷 Photo",
                    "video": "🎥 Video",
                    "audio": "🎤 Voice message",
                    "document": "📄 Document",
                    "sticker": "Sticker",
                }
                msg.body = labels.get(saved["media_type"], "📷 Photo")
                changed = True

            if changed:
                conv = await db.get(WhatsAppConversation, msg.conversation_id)
                if conv and (conv.last_message_preview or "").strip() in {
                    "Attachment",
                    "[media]",
                    "media",
                    "",
                    "Unsupported message",
                    "📷 Photo",
                    "📄 Document",
                }:
                    conv.last_message_preview = (msg.body or msg.media_name or "")[:255]
                lid = (fields.get("lid") or "").strip()
                if conv and lid and not (conv.customer_lid or "").strip():
                    conv.customer_lid = lid
                updated += 1
                print(
                    f"ok {msg.id} {ext} body={msg.body[:60]!r} "
                    f"type={msg.media_type!r} url={bool(msg.media_url)}"
                )
            else:
                print(f"noop {msg.id} {ext} body={msg.body[:40]!r}")

        await db.commit()
        print(f"Done. updated={updated}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
