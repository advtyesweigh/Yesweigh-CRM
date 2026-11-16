"""Backfill missing inbound media files from stored webhook Message payloads."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.crypto_util import decrypt_secret
from app.db import SessionLocal, engine
from app.evolution import _persist_inbound_media, _svc
from app.models import EvolutionWebhookEvent, WhatsAppInstance, WhatsAppMessage


async def main() -> None:
    async with SessionLocal() as db:
        events = (
            await db.execute(
                select(EvolutionWebhookEvent)
                .where(EvolutionWebhookEvent.event_type == "Message")
                .order_by(EvolutionWebhookEvent.id.desc())
                .limit(40)
            )
        ).scalars().all()
        _, svc = await _svc(db)
        ok = 0
        for ev in reversed(events):
            try:
                payload = json.loads(ev.payload or "{}")
            except json.JSONDecodeError:
                continue
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
            if info.get("IsFromMe"):
                continue
            ext_id = str(info.get("ID") or "")
            if not ext_id:
                continue
            msg = (
                await db.execute(
                    select(WhatsAppMessage).where(
                        WhatsAppMessage.external_message_id == ext_id,
                        WhatsAppMessage.direction == "in",
                    )
                )
            ).scalar_one_or_none()
            if not msg:
                continue
            if msg.media_url and msg.media_url.startswith("/api/whatsapp/media/"):
                continue
            inst = await db.get(WhatsAppInstance, msg.whatsapp_instance_id)
            if not inst:
                continue
            token = decrypt_secret(inst.encrypted_instance_token)
            try:
                saved = await _persist_inbound_media(svc, token, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"fail {ext_id}: {exc}")
                continue
            if not saved.get("media_url"):
                print(f"no file {ext_id} type={saved.get('media_type')}")
                continue
            msg.media_url = saved["media_url"]
            msg.media_type = saved.get("media_type") or msg.media_type
            msg.media_name = saved.get("media_name") or msg.media_name
            if not msg.body:
                labels = {
                    "image": "📷 Photo",
                    "video": "🎥 Video",
                    "audio": "🎤 Voice message",
                    "document": "📄 Document",
                    "sticker": "Sticker",
                }
                msg.body = labels.get(msg.media_type, "[media]")
            ok += 1
            print(f"saved {ext_id} -> {msg.media_url}")
        await db.commit()
        print(f"Done. updated={ok}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
