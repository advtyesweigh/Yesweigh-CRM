"""Re-ingest recent Evolution Message webhooks that were stored but not parsed."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.evolution import _ingest_webhook_message
from app.models import EvolutionWebhookEvent, WhatsAppInstance


async def main() -> None:
    async with SessionLocal() as db:
        events = (
            await db.execute(
                select(EvolutionWebhookEvent)
                .where(EvolutionWebhookEvent.event_type.in_(["Message", "SendMessage"]))
                .order_by(EvolutionWebhookEvent.id.desc())
                .limit(200)
            )
        ).scalars().all()
        print(f"Replaying {len(events)} webhook events…")
        ok = 0
        for ev in reversed(events):
            if not ev.whatsapp_instance_id:
                continue
            inst = await db.get(WhatsAppInstance, ev.whatsapp_instance_id)
            if not inst:
                continue
            try:
                payload = json.loads(ev.payload or "{}")
            except json.JSONDecodeError:
                continue
            try:
                await _ingest_webhook_message(db, inst, payload, ev.event_type)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"event {ev.id} failed: {exc}")
        await db.commit()
        print(f"Done. processed={ok}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
