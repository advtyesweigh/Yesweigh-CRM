"""Instance-scoped WhatsApp history sync via Evolution GO.

Evolution GO (whatsmeow) does **not** expose findChats/findMessages that return
full chat bodies over HTTP. Available surfaces:

- GET /user/contacts — address book (seed CRM conversations)
- GET /group/list|/group/myall — name existing @g.us CRM rows (no bulk seed)
- POST /chat/history-sync — on-demand peer history request; messages arrive later
  as MESSAGE webhooks and are ingested by ``evolution._ingest_webhook_message``
- On first LoggedIn, WhatsApp also pushes an automatic RECENT/INITIAL history
  stream which CRM already stores when webhooks are subscribed to MESSAGE

Limits: WhatsApp controls how much history a linked device receives (typically
days/weeks of recent chats, not multi-year archives). On-demand requests can
fail or return partial chunks; this job is resilient and logs partial failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select

from app.crypto_util import decrypt_secret
from app.db import SessionLocal
from app.evolution import (
    assign_conversation_to_instance_owner,
    merge_conversation_into,
    _find_conversations_for_peer,
    _is_lid_phone,
    _jid_lid,
    _prefer_canonical_conversation,
    _remember_lid,
)
from app.evolution_service import EvolutionService
from app.models import (
    EvolutionIntegration,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppInstanceActivityLog,
    WhatsAppMessage,
)

logger = logging.getLogger("crm.whatsapp_history_sync")

ORG = "yesweigh"
_running: set[int] = set()
_lock = asyncio.Lock()

# Tunables — keep gentle on WhatsApp / Evolution rate limits + API mem (512m)
CONTACT_HISTORY_LIMIT = 40
HISTORY_COUNT_PER_CHAT = 40
HISTORY_REQUEST_DELAY_S = 0.5
POST_LOGIN_DELAY_S = 8.0
AUTO_RESYNC_COOLDOWN = timedelta(hours=6)


def _digits(value: str) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


def _jid_user_server(jid: str) -> tuple[str, str] | None:
    raw = (jid or "").strip()
    if "@" not in raw:
        return None
    user, _, server = raw.partition("@")
    user = user.split(":")[0]
    if not user or not server:
        return None
    return user, server


def _normalize_contact_rows(payload: Any) -> list[dict[str, Any]]:
    """Flatten Evolution GO /user/contacts response into {jid, phone, name}."""
    root = payload
    if isinstance(payload, dict):
        root = payload.get("data", payload)
    rows: list[Any]
    if isinstance(root, list):
        rows = root
    elif isinstance(root, dict):
        for key in ("contacts", "Contacts", "users", "data", "items"):
            if isinstance(root.get(key), list):
                rows = root[key]
                break
        else:
            # map jid -> contact object
            rows = list(root.values()) if root else []
    else:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        jid = str(
            item.get("Jid")
            or item.get("jid")
            or item.get("id")
            or item.get("ID")
            or item.get("TheirJID")
            or item.get("theirJid")
            or ""
        ).strip()
        if not jid and item.get("User") and item.get("Server"):
            jid = f"{item['User']}@{item['Server']}"
        if not jid and isinstance(item.get("Found"), dict):
            found = item["Found"]
            jid = str(found.get("Jid") or found.get("jid") or "")
        if not jid or jid in seen:
            continue
        # Skip groups / broadcasts / status / newsletters for 1:1 inbox
        lower = jid.lower()
        if any(x in lower for x in ("@g.us", "@broadcast", "status@", "@newsletter")):
            continue
        phone = _digits(jid.split("@")[0].split(":")[0])
        if "@lid" in lower:
            # Prefer redacted / linked phone when present
            alt = str(
                item.get("RedactedPhone")
                or item.get("redactedPhone")
                or item.get("Phone")
                or item.get("phone")
                or ""
            )
            alt_digits = _digits(alt)
            if alt_digits:
                phone = alt_digits
        name = str(
            item.get("FullName")
            or item.get("fullName")
            or item.get("PushName")
            or item.get("pushName")
            or item.get("BusinessName")
            or item.get("businessName")
            or item.get("FirstName")
            or item.get("firstName")
            or item.get("Name")
            or item.get("name")
            or ""
        ).strip()
        if not phone and not jid:
            continue
        seen.add(jid)
        out.append({"jid": jid, "phone": phone or _digits(jid.split("@")[0]), "name": name})
    return out


async def _get_svc(db) -> EvolutionService | None:
    integ = (
        await db.execute(
            select(EvolutionIntegration)
            .where(EvolutionIntegration.organization_id == ORG)
            .order_by(EvolutionIntegration.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not integ or not integ.enabled:
        return None
    from app.config import get_settings

    key = decrypt_secret(integ.encrypted_api_key) or get_settings().evolution_api_key
    if not key:
        return None
    url = integ.server_url or get_settings().evolution_base_url
    return EvolutionService(url, key)


async def _upsert_contact_conversation(
    db, inst: WhatsAppInstance, *, phone: str, jid: str, name: str
) -> WhatsAppConversation | None:
    if not phone and not jid:
        return None
    lid = _jid_lid(jid) or (_digits(phone) if _is_lid_phone(phone) else "")
    real_phone = "" if _is_lid_phone(phone) else _digits(phone)
    peers = await _find_conversations_for_peer(
        db, inst.id, phone=real_phone or phone, jid=jid, lid=lid
    )
    conv = _prefer_canonical_conversation(peers)
    if conv and len(peers) > 1:
        for other in peers:
            if other.id == conv.id:
                continue
            other_jid = (other.customer_jid or "").strip()
            if other_jid.endswith("@lid") or _is_lid_phone(other.customer_phone or ""):
                await merge_conversation_into(db, keep=conv, absorb=other)
    if conv:
        if name and (not conv.customer_name or conv.customer_name == conv.customer_phone):
            conv.customer_name = name
        if real_phone and (not conv.customer_phone or _is_lid_phone(conv.customer_phone)):
            conv.customer_phone = real_phone
        if real_phone and not (conv.customer_jid or "").endswith("@s.whatsapp.net"):
            conv.customer_jid = f"{real_phone}@s.whatsapp.net"
        elif jid and not (conv.customer_jid or "").endswith("@s.whatsapp.net") and "@lid" not in jid:
            conv.customer_jid = jid
        if lid:
            _remember_lid(conv, lid)
        if not conv.assigned_employee_id:
            await assign_conversation_to_instance_owner(db, conv, inst.id)
        return conv
    # Skip creating a second shell when this is a pure LID contact with no PN yet —
    # inbound/SendMessage webhooks will attach the LID to the PN chat via customer_lid.
    # Still create when we only have LID so the contact appears after a fresh sync.
    store_phone = real_phone or phone or _digits(jid.split("@")[0])
    store_jid = jid
    if real_phone:
        store_jid = f"{real_phone}@s.whatsapp.net"
    elif lid:
        store_jid = f"{lid}@lid"
        store_phone = lid
    conv = WhatsAppConversation(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        whatsapp_instance_id=inst.id,
        customer_phone=store_phone,
        customer_name=name,
        customer_jid=store_jid,
        customer_lid=lid or "",
        status="Open",
        tag="Synced",
        last_message_preview="",
        unread_count=0,
    )
    db.add(conv)
    await db.flush()
    await assign_conversation_to_instance_owner(db, conv, inst.id)
    return conv


async def _oldest_message_for_chat(db, inst_id: int, conv_id: int) -> WhatsAppMessage | None:
    return (
        await db.execute(
            select(WhatsAppMessage)
            .where(
                WhatsAppMessage.whatsapp_instance_id == inst_id,
                WhatsAppMessage.conversation_id == conv_id,
                WhatsAppMessage.external_message_id != "",
            )
            .order_by(WhatsAppMessage.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_history_sync(
    instance_db_id: int,
    *,
    trigger: str = "auto",
    force: bool = False,
) -> dict[str, Any]:
    """Fetch contacts + request on-demand history for one CRM WhatsApp instance."""
    async with _lock:
        if instance_db_id in _running and not force:
            return {"ok": False, "skipped": True, "reason": "already_running"}
        # force=True preempts a stuck in-memory / crashed job
        _running.discard(instance_db_id)
        _running.add(instance_db_id)

    stats: dict[str, Any] = {
        "ok": True,
        "trigger": trigger,
        "contacts_seen": 0,
        "conversations_upserted": 0,
        "history_requested": 0,
        "history_failed": 0,
        "errors": [],
    }
    try:
        async with SessionLocal() as db:
            inst = await db.get(WhatsAppInstance, instance_db_id)
            if not inst or inst.deleted_at is not None or inst.organization_id != ORG:
                stats["ok"] = False
                stats["errors"].append("instance_not_found")
                return stats

            if not force and (inst.history_sync_status or "") == "running":
                # Recover stale "running" left by API restart mid-job
                stale = True
                if inst.history_sync_at:
                    last = inst.history_sync_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    stale = datetime.now(timezone.utc) - last > timedelta(minutes=20)
                if not stale:
                    stats["ok"] = False
                    stats["skipped"] = True
                    stats["reason"] = "db_running"
                    return stats

            if (
                not force
                and trigger == "auto"
                and inst.history_sync_status == "completed"
                and inst.history_sync_at
            ):
                last = inst.history_sync_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last < AUTO_RESYNC_COOLDOWN:
                    stats["ok"] = True
                    stats["skipped"] = True
                    stats["reason"] = "cooldown"
                    return stats

            inst.history_sync_status = "running"
            inst.history_sync_error = ""
            inst.history_sync_at = datetime.now(timezone.utc)
            await db.commit()

            svc = await _get_svc(db)
            token = decrypt_secret(inst.encrypted_instance_token)
            if not svc or not token:
                inst.history_sync_status = "error"
                inst.history_sync_error = "Evolution service or instance token missing"
                await db.commit()
                stats["ok"] = False
                stats["errors"].append(inst.history_sync_error)
                return stats

            # Give pairing / automatic RECENT history a moment to land via webhooks
            if trigger == "auto":
                await asyncio.sleep(POST_LOGIN_DELAY_S)

            contacts: list[dict[str, Any]] = []
            try:
                raw_contacts = await svc.get_contacts(token)
                contacts = _normalize_contact_rows(raw_contacts)
            except Exception as exc:  # noqa: BLE001
                msg = f"contacts: {exc}"
                logger.warning("history sync contacts failed instance=%s: %s", inst.public_id, exc)
                stats["errors"].append(msg)

            stats["contacts_seen"] = len(contacts)
            for row in contacts:
                try:
                    conv = await _upsert_contact_conversation(
                        db,
                        inst,
                        phone=row["phone"],
                        jid=row["jid"],
                        name=row["name"],
                    )
                    if conv:
                        stats["conversations_upserted"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["errors"].append(f"upsert {row.get('jid')}: {exc}")
            await db.commit()

            # Backfill names for existing @g.us rows only — do not seed every membership
            # (devices can have 80+ groups; live webhooks create rows when messages arrive).
            # Newsletters/channels stay out of the CRM inbox.
            try:
                from app.whatsapp_chat_types import parse_group_rows

                groups = await svc.list_groups(token)
                grown = parse_group_rows(groups)
                stats["groups_seen"] = len(grown)
                name_by_jid = {r["jid"].lower(): (r.get("name") or "").strip() for r in grown}
                existing_groups = (
                    await db.execute(
                        select(WhatsAppConversation).where(
                            WhatsAppConversation.whatsapp_instance_id == inst.id,
                            WhatsAppConversation.organization_id == ORG,
                            WhatsAppConversation.customer_jid.ilike("%@g.us"),
                        )
                    )
                ).scalars().all()
                named = 0
                for conv in existing_groups:
                    jid = (conv.customer_jid or "").strip().lower()
                    name = name_by_jid.get(jid) or ""
                    if name and (not conv.customer_name or conv.customer_name == conv.customer_phone):
                        conv.customer_name = name[:160]
                        named += 1
                stats["groups_named"] = named
                if named:
                    await db.commit()
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"groups: {exc}")

            # Prefer chats that already have CRM messages (Signal session likely),
            # then recently active / contact-seeded chats. Empty never-messaged
            # peers often fail with "no signal session established".
            targets: list[dict[str, Any]] = []
            seen_jids: set[str] = set()

            def _add_target(jid: str, phone: str = "", name: str = "") -> None:
                if not jid or jid in seen_jids or _jid_user_server(jid) is None:
                    return
                if "@g.us" in jid.lower() or "@broadcast" in jid.lower():
                    return
                if "@lid" in jid.lower():
                    phone_digits = _digits(phone) or _digits(jid)
                    if not phone_digits:
                        return
                    jid = f"{phone_digits}@s.whatsapp.net"
                    if jid in seen_jids:
                        return
                seen_jids.add(jid)
                targets.append({"jid": jid, "phone": _digits(phone) or _digits(jid), "name": name})

            # 1) Conversations that already have messages
            with_msgs = (
                await db.execute(
                    select(WhatsAppConversation)
                    .where(
                        WhatsAppConversation.whatsapp_instance_id == inst.id,
                        WhatsAppConversation.organization_id == ORG,
                        WhatsAppConversation.last_message_at.is_not(None),
                    )
                    .order_by(WhatsAppConversation.last_message_at.desc())
                    .limit(CONTACT_HISTORY_LIMIT)
                )
            ).scalars().all()
            for conv in with_msgs:
                jid = (conv.customer_jid or "").strip() or f"{_digits(conv.customer_phone)}@s.whatsapp.net"
                _add_target(jid, conv.customer_phone, conv.customer_name or "")

            # 2) Contacts from Evolution address book
            for row in contacts:
                if len(targets) >= CONTACT_HISTORY_LIMIT:
                    break
                _add_target(row.get("jid") or "", row.get("phone") or "", row.get("name") or "")

            # 3) Remaining DB conversations (empty Synced shells)
            if len(targets) < CONTACT_HISTORY_LIMIT:
                existing = (
                    await db.execute(
                        select(WhatsAppConversation)
                        .where(
                            WhatsAppConversation.whatsapp_instance_id == inst.id,
                            WhatsAppConversation.organization_id == ORG,
                        )
                        .order_by(
                            WhatsAppConversation.last_message_at.desc().nullslast(),
                            WhatsAppConversation.id.asc(),
                        )
                        .limit(CONTACT_HISTORY_LIMIT * 2)
                    )
                ).scalars().all()
                for conv in existing:
                    if len(targets) >= CONTACT_HISTORY_LIMIT:
                        break
                    jid = (conv.customer_jid or "").strip() or f"{_digits(conv.customer_phone)}@s.whatsapp.net"
                    _add_target(jid, conv.customer_phone, conv.customer_name or "")

            for row in targets[:CONTACT_HISTORY_LIMIT]:
                jid = row["jid"]
                if _jid_user_server(jid) is None:
                    continue
                msg_id = None
                from_me = None
                # Prefer conversation already in DB for this jid/phone.
                # Duplicate jid/phone shells are common after contact+LID merges —
                # never use scalar_one_or_none() here (crashes whole sync).
                try:
                    peers = (
                        await db.execute(
                            select(WhatsAppConversation).where(
                                WhatsAppConversation.whatsapp_instance_id == inst.id,
                                or_(
                                    WhatsAppConversation.customer_jid == jid,
                                    WhatsAppConversation.customer_phone == row.get("phone"),
                                ),
                            )
                        )
                    ).scalars().all()
                    conv = _prefer_canonical_conversation(list(peers))
                    if conv:
                        oldest = await _oldest_message_for_chat(db, inst.id, conv.id)
                        if oldest and oldest.external_message_id:
                            msg_id = oldest.external_message_id
                            from_me = oldest.direction == "out"
                except Exception as exc:  # noqa: BLE001
                    if len(stats["errors"]) < 40:
                        stats["errors"].append(f"lookup {jid}: {exc}")
                try:
                    await svc.request_history_sync(
                        token,
                        jid,
                        count=HISTORY_COUNT_PER_CHAT,
                        message_id=msg_id,
                        is_from_me=from_me,
                    )
                    stats["history_requested"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["history_failed"] += 1
                    if len(stats["errors"]) < 40:
                        stats["errors"].append(f"history {jid}: {exc}")
                await asyncio.sleep(HISTORY_REQUEST_DELAY_S)

            # Refresh instance row after long loop
            await db.refresh(inst)
            inst.history_sync_status = "completed"
            inst.history_sync_at = datetime.now(timezone.utc)
            inst.history_sync_stats = json.dumps(
                {k: v for k, v in stats.items() if k != "errors"} | {"error_count": len(stats["errors"])}
            )[:4000]
            if stats["errors"]:
                inst.history_sync_error = "; ".join(stats["errors"][:8])[:2000]
            else:
                inst.history_sync_error = ""
            db.add(
                WhatsAppInstanceActivityLog(
                    organization_id=ORG,
                    whatsapp_instance_id=inst.id,
                    action="history_sync",
                    event_type=trigger,
                    event_details=json.dumps(stats)[:4000],
                )
            )
            await db.commit()
            logger.info(
                "history sync done instance=%s trigger=%s contacts=%s hist_ok=%s hist_fail=%s",
                inst.public_id,
                trigger,
                stats["contacts_seen"],
                stats["history_requested"],
                stats["history_failed"],
            )
            return stats
    except Exception as exc:  # noqa: BLE001
        logger.exception("history sync crashed instance_id=%s", instance_db_id)
        stats["ok"] = False
        stats["errors"].append(str(exc))
        try:
            async with SessionLocal() as db:
                inst = await db.get(WhatsAppInstance, instance_db_id)
                if inst:
                    inst.history_sync_status = "error"
                    inst.history_sync_error = str(exc)[:2000]
                    inst.history_sync_at = datetime.now(timezone.utc)
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass
        return stats
    finally:
        async with _lock:
            _running.discard(instance_db_id)


def schedule_history_sync(
    instance_db_id: int,
    *,
    trigger: str = "auto",
    force: bool = False,
) -> None:
    """Fire-and-forget background task (safe from request handlers)."""

    async def _runner() -> None:
        try:
            await run_history_sync(instance_db_id, trigger=trigger, force=force)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled history sync failed instance_id=%s", instance_db_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_runner())
