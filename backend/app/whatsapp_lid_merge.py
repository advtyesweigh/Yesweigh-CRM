"""One-shot / maintenance: merge orphan @lid WhatsApp conversations into PN twins.

Root cause: Evolution GO SendMessage webhooks address chats by LID while inbound
MESSAGE events use @s.whatsapp.net (+ SenderAlt=@lid). History sync also seeds
separate Synced rows per jid. Result: two inbox rows (Open vs Synced) for one person.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.evolution import (
    ORG,
    _is_lid_phone,
    _jid_lid,
    _jid_phone,
    merge_conversation_into,
)
from app.models import EvolutionWebhookEvent, WhatsAppConversation

logger = logging.getLogger("crm.whatsapp_lid_merge")


def _lid_pn_from_payload(payload: dict[str, Any]) -> tuple[str, str] | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
    chat = str(info.get("Chat") or "")
    alts = [
        info.get("SenderAlt"),
        info.get("RecipientAlt"),
        data.get("JIDAlt"),
    ]
    phone = _jid_phone(chat)
    lid = _jid_lid(chat)
    for alt in alts:
        alt_s = str(alt or "")
        if not phone:
            phone = _jid_phone(alt_s)
        if not lid:
            lid = _jid_lid(alt_s)
    if phone and lid and phone != lid:
        return lid, phone
    return None


async def _build_lid_to_pn_from_webhooks(db: AsyncSession) -> dict[str, str]:
    mapping: dict[str, str] = {}
    rows = (
        await db.execute(
            select(EvolutionWebhookEvent.payload).where(
                or_(
                    EvolutionWebhookEvent.payload.ilike("%@lid%"),
                    EvolutionWebhookEvent.payload.ilike("%SenderAlt%"),
                )
            )
        )
    ).scalars().all()
    for raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            continue
        pair = _lid_pn_from_payload(payload if isinstance(payload, dict) else {})
        if not pair:
            continue
        lid, phone = pair
        mapping[lid] = phone
    return mapping


async def _merge_pair(db: AsyncSession, keep: WhatsAppConversation, absorb: WhatsAppConversation) -> bool:
    if keep.id == absorb.id:
        return False
    # Prefer PN as keep
    keep_is_lid = (keep.customer_jid or "").endswith("@lid") or _is_lid_phone(keep.customer_phone or "")
    absorb_is_lid = (absorb.customer_jid or "").endswith("@lid") or _is_lid_phone(absorb.customer_phone or "")
    if keep_is_lid and not absorb_is_lid:
        keep, absorb = absorb, keep
    await merge_conversation_into(db, keep=keep, absorb=absorb)
    return True


async def run_lid_pn_merge(db: AsyncSession) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "ok": True,
        "webhook_pairs": 0,
        "merged": 0,
        "customer_lid_set": 0,
        "errors": [],
    }
    lid_to_pn = await _build_lid_to_pn_from_webhooks(db)
    stats["webhook_pairs"] = len(lid_to_pn)

    # 1) Apply learned LID onto existing PN conversations + merge LID shells
    for lid, phone in lid_to_pn.items():
        try:
            pn_rows = (
                await db.execute(
                    select(WhatsAppConversation).where(
                        WhatsAppConversation.organization_id == ORG,
                        or_(
                            WhatsAppConversation.customer_phone == phone,
                            WhatsAppConversation.customer_jid == f"{phone}@s.whatsapp.net",
                        ),
                    )
                )
            ).scalars().all()
            lid_rows = (
                await db.execute(
                    select(WhatsAppConversation).where(
                        WhatsAppConversation.organization_id == ORG,
                        or_(
                            WhatsAppConversation.customer_lid == lid,
                            WhatsAppConversation.customer_phone == lid,
                            WhatsAppConversation.customer_jid == f"{lid}@lid",
                            WhatsAppConversation.customer_jid.like(f"{lid}:%@lid"),
                        ),
                    )
                )
            ).scalars().all()
            for pn in pn_rows:
                if not (pn.customer_lid or "").strip():
                    pn.customer_lid = lid
                    stats["customer_lid_set"] += 1
                for shell in lid_rows:
                    if shell.whatsapp_instance_id != pn.whatsapp_instance_id:
                        continue
                    if shell.id == pn.id:
                        continue
                    if await _merge_pair(db, pn, shell):
                        stats["merged"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"lid={lid}: {exc}")

    await db.flush()

    # 2) Merge same-instance rows that share an external_message_id
    dup_ext = (
        await db.execute(
            text(
                """
                SELECT m.whatsapp_instance_id, m.external_message_id,
                       array_agg(DISTINCT m.conversation_id ORDER BY m.conversation_id) AS conv_ids
                FROM whatsapp_messages m
                WHERE m.external_message_id <> ''
                GROUP BY m.whatsapp_instance_id, m.external_message_id
                HAVING count(DISTINCT m.conversation_id) > 1
                """
            )
        )
    ).mappings().all()
    for row in dup_ext:
        ids = list(row["conv_ids"] or [])
        if len(ids) < 2:
            continue
        convs = (
            await db.execute(select(WhatsAppConversation).where(WhatsAppConversation.id.in_(ids)))
        ).scalars().all()
        if len(convs) < 2:
            continue
        # Prefer PN
        convs_sorted = sorted(
            convs,
            key=lambda c: (
                0 if (c.customer_jid or "").endswith("@s.whatsapp.net") else 1,
                c.id,
            ),
        )
        keep = convs_sorted[0]
        for absorb in convs_sorted[1:]:
            try:
                if await _merge_pair(db, keep, absorb):
                    stats["merged"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"ext-merge {keep.id}/{absorb.id}: {exc}")

    await db.flush()

    # 3) Same display name + same instance where one is @lid and one is PN
    name_pairs = (
        await db.execute(
            text(
                """
                SELECT a.id AS a_id, b.id AS b_id
                FROM whatsapp_conversations a
                JOIN whatsapp_conversations b
                  ON a.whatsapp_instance_id = b.whatsapp_instance_id
                 AND a.id < b.id
                 AND lower(trim(a.customer_name)) = lower(trim(b.customer_name))
                 AND length(trim(a.customer_name)) >= 3
                 AND (
                   (a.customer_jid LIKE '%@lid' AND b.customer_jid LIKE '%@s.whatsapp.net')
                   OR (b.customer_jid LIKE '%@lid' AND a.customer_jid LIKE '%@s.whatsapp.net')
                 )
                WHERE a.organization_id = 'yesweigh'
                """
            )
        )
    ).mappings().all()
    for row in name_pairs:
        a = await db.get(WhatsAppConversation, row["a_id"])
        b = await db.get(WhatsAppConversation, row["b_id"])
        if not a or not b:
            continue
        try:
            if await _merge_pair(db, a, b):
                stats["merged"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"name-merge {row['a_id']}/{row['b_id']}: {exc}")

    await db.commit()
    return stats
