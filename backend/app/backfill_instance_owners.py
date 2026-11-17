"""Backfill unassigned WhatsApp conversations to their instance primary owner."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db import SessionLocal, engine
from app.evolution import ORG, assign_conversation_to_instance_owner, primary_owner_employee
from app.models import WhatsAppConversation, WhatsAppInstance


async def main() -> None:
    async with SessionLocal() as db:
        instances = (
            await db.execute(
                select(WhatsAppInstance).where(
                    WhatsAppInstance.organization_id == ORG,
                    WhatsAppInstance.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        updated = 0
        for inst in instances:
            owner = await primary_owner_employee(db, inst.id)
            if not owner:
                print(f"instance {inst.id} ({inst.display_name}): no linked employee — skip")
                continue
            rows = (
                await db.execute(
                    select(WhatsAppConversation).where(
                        WhatsAppConversation.organization_id == ORG,
                        WhatsAppConversation.whatsapp_instance_id == inst.id,
                        WhatsAppConversation.assigned_employee_id.is_(None),
                    )
                )
            ).scalars().all()
            print(
                f"instance {inst.id} ({inst.display_name}): "
                f"owner={owner.name} (#{owner.id}) unassigned={len(rows)}"
            )
            for conv in rows:
                if await assign_conversation_to_instance_owner(db, conv, inst.id):
                    updated += 1

        await db.commit()
        remaining = (
            await db.execute(
                select(func.count())
                .select_from(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.assigned_employee_id.is_(None),
                )
            )
        ).scalar_one()
        print(f"Done. updated={updated} remaining_unassigned={remaining}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
