"""Backfill missing media files / placeholder bodies from stored webhook payloads.

Repairs:
- media_type set but media_url empty (failed download)
- body in {Attachment, [media], media, Photo labels} with no usable media
- nested Evolution shells (questionReplyMessage / templateMessage)
"""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import or_, select

from app.crypto_util import decrypt_secret
from app.db import SessionLocal, engine
from app.evolution import _extract_message_fields, _persist_message_media, _svc
from app.models import EvolutionWebhookEvent, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage

MEDIA_LABELS = {
    "image": "📷 Photo",
    "video": "🎥 Video",
    "audio": "🎤 Voice message",
    "document": "📄 Document",
    "sticker": "Sticker",
}
PLACEHOLDER_BODIES = {
    "",
    "[media]",
    "media",
    "Attachment",
    "Unsupported message",
    *MEDIA_LABELS.values(),
}


async def main() -> None:
    async with SessionLocal() as db:
        missing = (
            await db.execute(
                select(WhatsAppMessage)
                .where(
                    or_(
                        WhatsAppMessage.media_type != "",
                        WhatsAppMessage.body.in_(tuple(PLACEHOLDER_BODIES)),
                    )
                )
                .order_by(WhatsAppMessage.id.desc())
                .limit(300)
            )
        ).scalars().all()

        need: list[WhatsAppMessage] = []
        for m in missing:
            url = (m.media_url or "").strip()
            body = (m.body or "").strip()
            if url.startswith("/api/whatsapp/media/") and body not in PLACEHOLDER_BODIES:
                continue
            if not url.startswith("/api/whatsapp/media/") or body in PLACEHOLDER_BODIES:
                need.append(m)

        need_by_ext = {m.external_message_id: m for m in need if m.external_message_id}
        print(f"candidates={len(need)} with_ext={len(need_by_ext)}")

        events = (
            await db.execute(
                select(EvolutionWebhookEvent)
                .where(EvolutionWebhookEvent.event_type == "Message")
                .order_by(EvolutionWebhookEvent.id.desc())
                .limit(800)
            )
        ).scalars().all()
        _, svc = await _svc(db)
        ok_media = 0
        ok_text = 0

        for ev in reversed(events):
            try:
                payload = json.loads(ev.payload or "{}")
            except json.JSONDecodeError:
                continue
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
            ext_id = str(info.get("ID") or "")
            if not ext_id or ext_id not in need_by_ext:
                continue

            msg = need_by_ext[ext_id]
            fields = _extract_message_fields(payload) or {}
            new_text = str(fields.get("text") or "").strip()
            new_media_type = str(fields.get("media_type") or "").strip()

            # Repair mislabeled text replies (Attachment / Photo with no real media).
            if new_text and new_text not in PLACEHOLDER_BODIES:
                if (msg.body or "").strip() in PLACEHOLDER_BODIES or not (msg.body or "").strip():
                    msg.body = new_text
                    ok_text += 1
                    conv = await db.get(WhatsAppConversation, msg.conversation_id)
                    if conv and (conv.last_message_preview or "").strip() in PLACEHOLDER_BODIES:
                        conv.last_message_preview = new_text[:255]

            inst = await db.get(WhatsAppInstance, msg.whatsapp_instance_id)
            if not inst:
                continue
            token = decrypt_secret(inst.encrypted_instance_token)
            try:
                saved = await _persist_message_media(svc, token, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"fail {ext_id}: {exc}")
                continue

            if saved.get("media_url"):
                msg.media_url = saved["media_url"]
                msg.media_type = saved.get("media_type") or msg.media_type or new_media_type
                msg.media_name = saved.get("media_name") or msg.media_name
                if (msg.body or "").strip() in PLACEHOLDER_BODIES:
                    msg.body = MEDIA_LABELS.get(msg.media_type or "", msg.body or "Attachment")
                ok_media += 1
                print(f"saved {ext_id} -> {msg.media_url} type={msg.media_type}")
            else:
                if saved.get("media_type") and not msg.media_type:
                    msg.media_type = saved["media_type"]
                if new_media_type and not msg.media_type:
                    msg.media_type = new_media_type
                if (msg.body or "").strip() in {"Attachment", "[media]", "media", ""} and msg.media_type:
                    msg.body = MEDIA_LABELS.get(msg.media_type, "Attachment")
                print(f"no file {ext_id} type={msg.media_type or saved.get('media_type')} body={msg.body[:40]!r}")

        await db.commit()
        print(f"Done. media_updated={ok_media} text_updated={ok_text}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
