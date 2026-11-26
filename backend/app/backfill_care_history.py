"""Bulk-import Care (Meta Cloud) message history from YesOne Firestore into CRM.

Auth: same Firebase login as catalog sync (FIREBASE_EMAIL + FIREBASE_PASSWORD on VPS).

Examples:
  PYTHONPATH=/app python -m app.backfill_care_history
  PYTHONPATH=/app python -m app.backfill_care_history --only-empty
  PYTHONPATH=/app python -m app.backfill_care_history --wa-id 919645022020
"""
from __future__ import annotations

import argparse
import asyncio

from fastapi import HTTPException
from sqlalchemy import func, select

from app.cloud_whatsapp import (
    get_care_instance,
    sync_care_from_firestore,
    sync_care_wa_ids_from_firestore,
)
from app.sync_firebase_catalog import FirebaseAuthError
from app.db import SessionLocal, engine
from app.models import WhatsAppConversation, WhatsAppMessage


def _digits(value: str) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


async def run(
    *,
    only_empty: bool = False,
    wa_id: str = "",
    conversation_limit: int = 300,
    message_limit: int = 200,
    pull_all: bool = True,
    batch_size: int = 25,
    sleep_s: float = 0.3,
) -> dict[str, int]:
    imported_msgs = 0
    chats_touched = 0
    async with SessionLocal() as db:
        inst = await get_care_instance(db)
        if not inst:
            print("No Care (meta_cloud) instance in CRM.")
            return {"chats": 0, "messages": 0, "empty_before": 0}

        if wa_id:
            wa = _digits(wa_id)
            async with SessionLocal() as sdb:
                result = await sync_care_from_firestore(
                    sdb,
                    message_wa_id=wa,
                    message_limit=message_limit,
                )
            print(result)
            return {
                "chats": int(result.get("conversations") or 0),
                "messages": int(result.get("messages") or 0),
                "empty_before": 0,
            }

        empty_ids: list[tuple[int, str]] = []
        if only_empty:
            subq = (
                select(WhatsAppMessage.conversation_id)
                .where(WhatsAppMessage.whatsapp_instance_id == inst.id)
                .distinct()
            )
            rows = (
                await db.execute(
                    select(WhatsAppConversation.id, WhatsAppConversation.customer_phone)
                    .where(
                        WhatsAppConversation.whatsapp_instance_id == inst.id,
                        ~WhatsAppConversation.id.in_(subq),
                    )
                    .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
                )
            ).all()
            empty_ids = [(int(r[0]), _digits(str(r[1] or ""))) for r in rows if _digits(str(r[1] or ""))]
            print(f"Care conversations with preview but zero CRM messages: {len(empty_ids)}", flush=True)
            phones = [p for _cid, p in empty_ids]
            for i in range(0, len(phones), batch_size):
                batch = phones[i : i + batch_size]
                for attempt in range(4):
                    try:
                        async with SessionLocal() as sdb:
                            result = await sync_care_wa_ids_from_firestore(
                                sdb,
                                batch,
                                message_limit=message_limit,
                            )
                        imported_msgs += int(result.get("messages") or 0)
                        chats_touched += len(batch)
                        print(
                            f"  … {chats_touched}/{len(phones)} chats, "
                            f"+{result.get('messages', 0)} msgs (total {imported_msgs})",
                            flush=True,
                        )
                        break
                    except (FirebaseAuthError, HTTPException) as exc:
                        err = str(getattr(exc, "detail", None) or exc)
                        if "QUOTA_EXCEEDED" in err and attempt < 3:
                            wait = 45 * (attempt + 1)
                            print(f"Firebase quota — sleep {wait}s then retry ({err[:80]})", flush=True)
                            await asyncio.sleep(wait)
                            continue
                        raise
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
        else:
            async with SessionLocal() as sdb:
                result = await sync_care_from_firestore(
                    sdb,
                    conversation_limit=conversation_limit,
                    message_limit=message_limit,
                    pull_all_messages=pull_all,
                )
            chats_touched = int(result.get("conversations") or 0)
            imported_msgs = int(result.get("messages") or 0)
            print(result)

        remaining = 0
        if only_empty:
            async with SessionLocal() as db2:
                subq = (
                    select(WhatsAppMessage.conversation_id)
                    .where(WhatsAppMessage.whatsapp_instance_id == inst.id)
                    .distinct()
                )
                remaining = (
                    await db2.execute(
                        select(func.count())
                        .select_from(WhatsAppConversation)
                        .where(
                            WhatsAppConversation.whatsapp_instance_id == inst.id,
                            ~WhatsAppConversation.id.in_(subq),
                        )
                    )
                ).scalar() or 0

    return {
        "chats": chats_touched,
        "messages": imported_msgs,
        "empty_remaining": remaining if only_empty else 0,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill Care WhatsApp history from YesOne Firestore")
    p.add_argument("--only-empty", action="store_true", help="Import messages only for CRM chats with zero rows")
    p.add_argument("--wa-id", default="", help="Single customer waId / phone")
    p.add_argument("--conversation-limit", type=int, default=300)
    p.add_argument("--message-limit", type=int, default=200)
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--sleep", type=float, default=0.25, help="Delay between single-chat pulls")
    args = p.parse_args()
    out = asyncio.run(
        run(
            only_empty=args.only_empty,
            wa_id=args.wa_id,
            conversation_limit=args.conversation_limit,
            message_limit=args.message_limit,
            batch_size=args.batch_size,
            sleep_s=args.sleep,
        )
    )
    print(out)


if __name__ == "__main__":
    try:
        main()
    finally:
        asyncio.run(engine.dispose())