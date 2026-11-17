"""Backfill Care (meta_cloud) media onto VPS disk.

Downloads YesOne Firebase Storage URLs (or Meta media ids if present) into
/data/wa-media and rewrites whatsapp_messages.media_url to /api/whatsapp/media/...
"""
from __future__ import annotations

import asyncio

from sqlalchemy import or_, select

from app.cloud_whatsapp import (
    PROVIDER_META_CLOUD,
    get_care_instance,
    is_local_media_url,
    persist_care_media,
)
from app.db import SessionLocal, engine
from app.models import WhatsAppMessage

MEDIA_LABELS = {
    "image": "📷 Photo",
    "video": "🎥 Video",
    "audio": "🎤 Voice message",
    "document": "📄 Document",
    "sticker": "Sticker",
}


async def main() -> None:
    async with SessionLocal() as db:
        inst = await get_care_instance(db)
        if not inst:
            print("no Care meta_cloud instance")
            return
        print(f"Care instance id={inst.id} provider={inst.provider or PROVIDER_META_CLOUD}")

        rows = (
            await db.execute(
                select(WhatsAppMessage)
                .where(
                    WhatsAppMessage.whatsapp_instance_id == inst.id,
                    WhatsAppMessage.media_type != "",
                    or_(
                        WhatsAppMessage.media_url == "",
                        WhatsAppMessage.media_url.is_(None),
                        ~WhatsAppMessage.media_url.like("/api/whatsapp/media/%"),
                    ),
                )
                .order_by(WhatsAppMessage.id.desc())
                .limit(500)
            )
        ).scalars().all()
        print(f"candidates needing local media: {len(rows)}")

        ok = 0
        failed = 0
        for msg in rows:
            if is_local_media_url(msg.media_url or ""):
                continue
            try:
                saved = await persist_care_media(
                    media_url=msg.media_url or "",
                    media_type=msg.media_type or "",
                    media_name=msg.media_name or "",
                    msg_type=msg.media_type or "",
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"fail id={msg.id}: {exc}")
                continue
            local = saved.get("media_url") or ""
            if not is_local_media_url(local):
                failed += 1
                print(
                    f"no file id={msg.id} type={msg.media_type} "
                    f"src={(msg.media_url or '')[:60]!r}"
                )
                continue
            msg.media_url = local
            if saved.get("media_type") and not msg.media_type:
                msg.media_type = saved["media_type"]
            if saved.get("media_name"):
                msg.media_name = saved["media_name"]
            if not msg.body or msg.body in {"", "[media]", "media", "Attachment", *MEDIA_LABELS.values()}:
                msg.body = MEDIA_LABELS.get(msg.media_type or "", "Attachment")
            ok += 1
            print(f"saved id={msg.id} type={msg.media_type} -> {msg.media_url}")
            if (msg.media_type or "").lower() in {"audio", "ptt", "voice"} and msg.media_url:
                try:
                    from app.sarvam_transcribe import schedule_transcription

                    schedule_transcription(msg.id)
                except Exception as exc:  # noqa: BLE001
                    print(f"stt schedule skip id={msg.id}: {exc}")

        await db.commit()
        print(f"Done. updated={ok} failed={failed} scanned={len(rows)}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
