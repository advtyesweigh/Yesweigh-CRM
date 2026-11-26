"""Create Sarvam voice clones for Biju / Visakh / Shibin from outbound samples.

Run inside the API container (has /data/wa-media + SARVAM_API_KEY):

  python -m app.bootstrap_voice_clones

Does NOT enable voice_tts_enabled (stays false until admin opt-in).
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.models import WhatsAppInstance, WhatsAppMessage
from app.sarvam_voice import (
    ACCEPTABLE_DURATION_S,
    create_voice,
    duration_score,
    ogg_opus_duration_seconds,
    resolve_media_file,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bootstrap_voice_clones")

# Display-name match → preferred clone name
TARGETS = (
    ("Biju", "YW Biju B"),
    ("Visakh", "YW Visakh B"),
    ("Visak", "YW Visakh B"),
    ("Shibin", "YW Shibin K P"),
)


async def _best_outbound_sample(db, instance_id: int) -> tuple[WhatsAppMessage, float] | None:
    msgs = (
        await db.execute(
            select(WhatsAppMessage)
            .where(
                WhatsAppMessage.whatsapp_instance_id == instance_id,
                WhatsAppMessage.direction == "out",
                WhatsAppMessage.media_type.in_(["audio", "ptt", "voice"]),
                WhatsAppMessage.media_url.like("/api/whatsapp/media/%"),
            )
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(120)
        )
    ).scalars().all()

    best: tuple[WhatsAppMessage, float, float] | None = None  # msg, score, dur
    for msg in msgs:
        path = resolve_media_file(msg.media_url)
        if not path:
            continue
        dur = ogg_opus_duration_seconds(path)
        score = duration_score(dur)
        if score < 0 or dur is None:
            continue
        if best is None or score > best[1]:
            best = (msg, score, dur)
    if not best:
        return None
    return best[0], best[2]


async def main() -> None:
    seen_ids: set[int] = set()
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(WhatsAppInstance).where(
                    WhatsAppInstance.deleted_at.is_(None),
                    WhatsAppInstance.provider == "evolution",
                )
            )
        ).scalars().all()

        for needle, voice_name in TARGETS:
            matches = [
                r
                for r in rows
                if needle.lower() in (r.display_name or "").lower()
                or needle.lower() in (r.employee_name or "").lower()
            ]
            for inst in matches:
                if inst.id in seen_ids:
                    continue
                seen_ids.add(inst.id)
                pick = await _best_outbound_sample(db, inst.id)
                if not pick:
                    logger.error(
                        "No suitable outbound sample for %s (need %.0f–%.0fs)",
                        inst.display_name,
                        ACCEPTABLE_DURATION_S[0],
                        ACCEPTABLE_DURATION_S[1],
                    )
                    continue
                msg, dur = pick
                path = resolve_media_file(msg.media_url)
                assert path is not None
                logger.info(
                    "Creating clone for %s from msg=%s file=%s dur=%.2fs",
                    inst.display_name,
                    msg.id,
                    path.name,
                    dur,
                )
                try:
                    data = await create_voice(
                        file_path=path,
                        name=voice_name,
                        language="ml-IN",
                        gender="male",
                        style="conversational",
                        remove_background_noise=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Sarvam create failed for %s: %s", inst.display_name, exc)
                    continue
                voice_id = str(data.get("voice_id") or "")
                if not voice_id:
                    logger.error("Empty voice_id for %s: %s", inst.display_name, data)
                    continue
                from datetime import datetime, timezone

                inst.sarvam_voice_id = voice_id
                inst.sarvam_voice_name = voice_name
                inst.sarvam_voice_sample_url = msg.media_url
                inst.sarvam_voice_sample_message_id = msg.id
                inst.sarvam_voice_created_at = datetime.now(timezone.utc)
                # Keep TTS off
                inst.voice_tts_enabled = False
                await db.commit()
                logger.info(
                    "OK %s → %s (sample msg %s, %.2fs, noise_removed=%s)",
                    inst.display_name,
                    voice_id,
                    msg.id,
                    dur,
                    data.get("background_noise_removed"),
                )

    logger.info("Done. Enable Voice TTS per line in Settings → Voice Clones when ready.")


if __name__ == "__main__":
    asyncio.run(main())
