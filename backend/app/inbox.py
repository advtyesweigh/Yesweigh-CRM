"""Multi-agent WhatsApp Inbox APIs — YesWeigh org instances only."""
from __future__ import annotations

import asyncio
import base64
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth import get_current_user
from app.crypto_util import decrypt_secret
from app.db import get_db
from app.evolution import (
    ORG,
    _owned_instance,
    _require_admin,
    _svc,
    assign_conversation_to_instance_owner,
    clear_conversation_unread,
    primary_owner_employee,
)
from app.media_store import PUBLIC_PREFIX, resolve_path, save_bytes, save_data_url, to_evolution_base64
from app.dealers import _normalized_phone_sql, dealer_lifetime_sales_map
from app.models import (
    CampaignRun,
    Dealer,
    Employee,
    Lead,
    User,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppInstanceEmployee,
    WhatsAppMessage,
    WorkItem,
    ZohoDealer,
)
from app.phone_util import normalize_in_mobile, to_evolution_number
from app.whatsapp_chat_meta import chat_kind_from_jid, enrich_conversation_meta
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter
from app.whatsapp_service_meta import (
    enrich_conversation_rows,
    hold_since_map,
    parse_cached_outline_summary,
    resolve_service_fields,
)

router = APIRouter(tags=["whatsapp-inbox"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _activity_at():
    """Sort key so named groups without synced messages still surface near repair time."""
    return func.coalesce(
        WhatsAppConversation.last_message_at,
        WhatsAppConversation.updated_at,
        WhatsAppConversation.created_at,
    )


def _named_group_filter():
    """Real @g.us chats that already have a subject (not raw id / empty)."""
    name = func.trim(func.coalesce(WhatsAppConversation.customer_name, ""))
    phone = func.trim(func.coalesce(WhatsAppConversation.customer_phone, ""))
    jid_user = func.split_part(WhatsAppConversation.customer_jid, "@", 1)
    return and_(
        WhatsAppConversation.customer_jid.ilike("%@g.us"),
        name != "",
        name != phone,
        name != jid_user,
    )


def _lid_orphan_hidden_filter():
    """SQL predicate: hide @lid shells that already have a PN twin (same customer_lid).

    Prefer ``_drop_lid_orphans`` for list endpoints — this correlated EXISTS is too
    expensive under ORDER BY + LIMIT / COUNT on ~8k rows.
    """
    twin = aliased(WhatsAppConversation)
    self_lid = func.nullif(func.trim(WhatsAppConversation.customer_lid), "")
    twin_is_pn = or_(
        twin.customer_jid.ilike("%@s.whatsapp.net"),
        and_(
            ~twin.customer_jid.ilike("%@lid"),
            ~twin.customer_jid.ilike("%@g.us"),
            ~twin.customer_jid.ilike("%@newsletter"),
        ),
    )
    has_pn_twin = (
        select(twin.id)
        .where(
            twin.organization_id == WhatsAppConversation.organization_id,
            twin.whatsapp_instance_id == WhatsAppConversation.whatsapp_instance_id,
            twin.id != WhatsAppConversation.id,
            twin_is_pn,
            self_lid.is_not(None),
            twin.customer_lid == self_lid,
            twin.customer_lid != "",
        )
        .correlate(WhatsAppConversation)
        .exists()
    )
    return ~and_(
        WhatsAppConversation.customer_jid.ilike("%@lid"),
        self_lid.is_not(None),
        has_pn_twin,
    )


async def _drop_lid_orphans(
    db: AsyncSession, rows: list[WhatsAppConversation]
) -> list[WhatsAppConversation]:
    """Drop @lid rows that already have a non-@lid twin with the same customer_lid.

    Runs a small IN-list lookup on the page only — keeps inbox list <2s.
    """
    candidates = [
        r
        for r in rows
        if "@lid" in (r.customer_jid or "").lower() and (r.customer_lid or "").strip()
    ]
    if not candidates:
        return rows

    lids_by_inst: dict[int, set[str]] = {}
    for r in candidates:
        lids_by_inst.setdefault(r.whatsapp_instance_id, set()).add((r.customer_lid or "").strip())

    twin_keys: set[tuple[int, str]] = set()
    for inst_id, lids in lids_by_inst.items():
        twin_rows = (
            await db.execute(
                select(WhatsAppConversation.whatsapp_instance_id, WhatsAppConversation.customer_lid).where(
                    WhatsAppConversation.whatsapp_instance_id == inst_id,
                    WhatsAppConversation.customer_lid.in_(list(lids)),
                    WhatsAppConversation.customer_lid != "",
                    ~WhatsAppConversation.customer_jid.ilike("%@lid"),
                )
            )
        ).all()
        for iid, lid in twin_rows:
            twin_keys.add((int(iid), (lid or "").strip()))

    out: list[WhatsAppConversation] = []
    for r in rows:
        jid = (r.customer_jid or "").lower()
        lid = (r.customer_lid or "").strip()
        if "@lid" in jid and lid and (r.whatsapp_instance_id, lid) in twin_keys:
            continue
        out.append(r)
    return out


def _is_admin(user: User) -> bool:
    return user.role.lower() in {"admin", "super_admin", "super admin", "management"}


def _not_spam_filter():
    """Hide soft-spam conversations from normal inbox lists and badge counts."""
    return or_(
        WhatsAppConversation.is_spam.is_(False),
        WhatsAppConversation.is_spam.is_(None),
    )


def _phone_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _send_recipient(conv: WhatsAppConversation) -> str:
    """
    Evolution GO needs a real PN (with country code) or an explicit @lid jid.
    Bare LID digits get wrongly treated as @s.whatsapp.net and fail.
    Indian 10-digit mobiles must be sent as 91XXXXXXXXXX — otherwise Evolution
    formatJid turns e.g. 7592052528 into +7592052528 (wrong country) and fails.
    """
    jid = (conv.customer_jid or "").strip()
    phone = _phone_digits(conv.customer_phone)
    if jid.endswith("@lid"):
        lid = _phone_digits(jid.split("@")[0])
        # Prefer real phone when we have it and it differs from the LID
        if phone and phone != lid and len(phone) <= 15:
            return to_evolution_number(phone)
        return f"{lid}@lid" if lid else jid
    if phone:
        return to_evolution_number(phone)
    if "@" in jid:
        user = jid.split("@", 1)[0]
        # Repair bare 10-digit @s.whatsapp.net stored without country code
        if jid.endswith("@s.whatsapp.net"):
            fixed = to_evolution_number(user)
            if fixed and "@" not in fixed:
                return fixed
        return jid
    return to_evolution_number(conv.customer_phone) or (conv.customer_phone or "")

def _initials(name: str, phone: str = "") -> str:
    parts = [p for p in (name or "").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    if parts:
        return parts[0][:2].upper()
    digits = _phone_digits(phone)
    return (digits[-2:] if len(digits) >= 2 else "WA").upper()


def _color_for(key: str) -> str:
    palette = ["#e1ebff", "#e9e5fc", "#e3f5ee", "#fcead9", "#e4effa", "#f4e8fa", "#e1f1ec", "#edf0f5"]
    return palette[sum(ord(c) for c in (key or "x")) % len(palette)]


async def _employee_for_user(db: AsyncSession, user: User) -> Employee | None:
    return (
        await db.execute(select(Employee).where(or_(Employee.user_id == user.id, Employee.email == user.email)))
    ).scalar_one_or_none()


async def _authorized_instance_ids(db: AsyncSession, user: User) -> list[int] | None:
    """None = all YesWeigh instances (admin). Else list of allowed instance PKs."""
    if _is_admin(user):
        return None
    emp = await _employee_for_user(db, user)
    # Care (Meta Cloud) is a shared customer-care line — visible to all inbox users.
    care_ids = (
        await db.execute(
            select(WhatsAppInstance.id).where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
                WhatsAppInstance.provider == "meta_cloud",
            )
        )
    ).scalars().all()
    if not emp:
        return list(care_ids)
    rows = (
        await db.execute(
            select(WhatsAppInstanceEmployee.whatsapp_instance_id).where(
                WhatsAppInstanceEmployee.employee_id == emp.id,
                WhatsAppInstanceEmployee.is_active.is_(True),
                WhatsAppInstanceEmployee.organization_id == ORG,
            )
        )
    ).scalars().all()
    return list({*rows, *care_ids})


async def _get_conversation(db: AsyncSession, public_id: str, user: User) -> WhatsAppConversation:
    conv = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.public_id == public_id,
                WhatsAppConversation.organization_id == ORG,
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversation not found")
    allowed = await _authorized_instance_ids(db, user)
    if allowed is not None and conv.whatsapp_instance_id not in allowed:
        raise HTTPException(403, "Not authorized for this conversation")
    return conv


def _dealer_brief(dealer: Dealer | None, *, total_sales: float | None = None) -> dict[str, Any] | None:
    if not dealer:
        return None
    out: dict[str, Any] = {
        "id": dealer.id,
        "name": dealer.name or "",
        "company": dealer.company or "",
        "phone": dealer.phone or "",
        "code": dealer.zoho_contact_id or "",
        "city": dealer.city or "",
        "state": dealer.state or "",
    }
    if total_sales is not None:
        out["total_sales"] = round(float(total_sales), 2)
    return out


def _conv_out(
    conv: WhatsAppConversation,
    *,
    account: str = "",
    agent: str = "",
    dealer: Dealer | None = None,
    dealer_sales_total: float | None = None,
    provider: str = "",
    last_inbound_at: datetime | None = None,
    crm_contact_kind: str | None = None,
) -> dict[str, Any]:
    name = conv.customer_name or conv.customer_phone
    linked = _dealer_brief(dealer, total_sales=dealer_sales_total)
    customer_type = conv.customer_type or "Customer"
    if linked and customer_type.lower() != "dealer":
        customer_type = "Dealer"
    kind = chat_kind_from_jid(conv.customer_jid or "")
    out: dict[str, Any] = {
        "id": conv.public_id,
        "customer_name": conv.customer_name,
        "customer_phone": conv.customer_phone,
        "customer_photo": conv.customer_photo or "",
        "customer_jid": conv.customer_jid or "",
        "chat_kind": kind,
        "is_group": kind == "group",
        "is_channel": kind == "channel",
        "display_name": name,
        "initial": _initials(conv.customer_name, conv.customer_phone),
        "color": _color_for(conv.customer_phone or conv.public_id),
        "status": conv.status,
        "priority": conv.priority,
        "tag": conv.tag or ("New" if conv.status == "New" else conv.status),
        "assigned_employee_id": conv.assigned_employee_id,
        "assigned_agent": agent,
        "account": account,
        "whatsapp_instance_id": conv.whatsapp_instance_id,
        "last_message_preview": conv.last_message_preview,
        "last_message_at": conv.last_message_at,
        "unread_count": conv.unread_count,
        "language": conv.language,
        "region": conv.region,
        "customer_type": customer_type,
        "dealer_id": conv.dealer_id,
        "dealer": linked,
        "crm_contact_kind": crm_contact_kind,
        "notes_summary": conv.notes_summary,
        "created_at": conv.created_at,
        "onboarding_step": getattr(conv, "onboarding_step", None) or "",
        "onboarding_at": getattr(conv, "onboarding_at", None),
        "onboarding_lead_id": getattr(conv, "onboarding_lead_id", None),
    }
    # Care Meta Cloud only — Evolution personal lines never get Open/Expired.
    if (provider or "").strip().lower() == "meta_cloud":
        from app.cloud_whatsapp import care_customer_care_window

        out.update(care_customer_care_window(last_inbound_at))
    return out


async def _last_inbound_at_map(db: AsyncSession, conversation_ids: list[int]) -> dict[int, datetime]:
    """Max created_at of direction=in messages per conversation (CRM-local clock)."""
    if not conversation_ids:
        return {}
    rows = (
        await db.execute(
            select(WhatsAppMessage.conversation_id, func.max(WhatsAppMessage.created_at))
            .where(
                WhatsAppMessage.conversation_id.in_(conversation_ids),
                WhatsAppMessage.direction == "in",
            )
            .group_by(WhatsAppMessage.conversation_id)
        )
    ).all()
    return {int(cid): ts for cid, ts in rows if cid is not None and ts is not None}


async def _last_inbound_at(db: AsyncSession, conversation_id: int) -> datetime | None:
    return (
        await db.execute(
            select(func.max(WhatsAppMessage.created_at)).where(
                WhatsAppMessage.conversation_id == conversation_id,
                WhatsAppMessage.direction == "in",
            )
        )
    ).scalar_one_or_none()


async def _conv_out_with_instance(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    agent: str = "",
    dealer: Dealer | None = None,
    inst: WhatsAppInstance | None = None,
    crm_contact_kind: str | None = None,
) -> dict[str, Any]:
    if inst is None:
        inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    provider = ((getattr(inst, "provider", None) or "evolution") if inst else "").strip().lower()
    last_in = await _last_inbound_at(db, conv.id) if provider == "meta_cloud" else None
    if crm_contact_kind is None:
        crm_contact_kind = await _crm_contact_kind(db, conv, dealer=dealer)
    dealer_sales_total: float | None = None
    if dealer:
        sales_map = await dealer_lifetime_sales_map(db, [dealer])
        dealer_sales_total = sales_map.get(dealer.id)
    return _conv_out(
        conv,
        account=inst.display_name if inst else "",
        agent=agent,
        dealer=dealer,
        dealer_sales_total=dealer_sales_total,
        provider=provider,
        last_inbound_at=last_in,
        crm_contact_kind=crm_contact_kind,
    )


async def _find_dealer_by_phone(db: AsyncSession, phone: str) -> Dealer | None:
    """Match WA number to dealer by last-10 digits (dealers.phone + zoho_dealers phone/mobile)."""
    key = normalize_in_mobile(phone)
    if not key:
        return None
    dealer = (
        await db.execute(
            select(Dealer).where(_normalized_phone_sql(Dealer.phone) == key).order_by(Dealer.id.asc()).limit(1)
        )
    ).scalar_one_or_none()
    if dealer:
        return dealer
    zd = (
        await db.execute(
            select(ZohoDealer)
            .where(
                ZohoDealer.dealer_id.is_not(None),
                or_(
                    _normalized_phone_sql(ZohoDealer.phone) == key,
                    _normalized_phone_sql(ZohoDealer.mobile) == key,
                ),
            )
            .order_by(ZohoDealer.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if zd and zd.dealer_id:
        return await db.get(Dealer, zd.dealer_id)
    return None


async def _find_lead_by_phone(db: AsyncSession, phone: str) -> Lead | None:
    """Match WA number to CRM lead by last-10 digits (leads.phone)."""
    key = normalize_in_mobile(phone)
    if not key:
        return None
    return (
        await db.execute(
            select(Lead).where(_normalized_phone_sql(Lead.phone) == key).order_by(Lead.id.asc()).limit(1)
        )
    ).scalar_one_or_none()


async def _crm_contact_kind(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    dealer: Dealer | None = None,
) -> str | None:
    """Return 'dealer' | 'lead' for inbox badge, or None if no CRM phone match."""
    if conv.dealer_id:
        linked = dealer if dealer and dealer.id == conv.dealer_id else await db.get(Dealer, conv.dealer_id)
        if linked:
            return "dealer"
    if chat_kind_from_jid(conv.customer_jid or "") == "group":
        return None
    key = normalize_in_mobile(conv.customer_phone)
    if not key:
        return None
    if await _find_dealer_by_phone(db, conv.customer_phone):
        return "dealer"
    if await _find_lead_by_phone(db, conv.customer_phone):
        return "lead"
    return None


async def _crm_contact_kinds_map(
    db: AsyncSession,
    rows: list[WhatsAppConversation],
    *,
    dealer_map: dict[int, Dealer],
) -> dict[int, str]:
    """Batch resolver for list endpoint — dealer link / phone wins over lead."""
    kinds: dict[int, str] = {}
    pending: dict[int, str] = {}

    for conv in rows:
        if conv.dealer_id and conv.dealer_id in dealer_map:
            kinds[conv.id] = "dealer"
            continue
        if chat_kind_from_jid(conv.customer_jid or "") == "group":
            continue
        key = normalize_in_mobile(conv.customer_phone)
        if key:
            pending[conv.id] = key

    if not pending:
        return kinds

    keys = list({k for k in pending.values()})
    dealer_keys: set[str] = set()
    for (p10,) in (
        await db.execute(
            select(_normalized_phone_sql(Dealer.phone)).where(_normalized_phone_sql(Dealer.phone).in_(keys))
        )
    ).all():
        if p10:
            dealer_keys.add(str(p10))
    for phone_p10, mobile_p10 in (
        await db.execute(
            select(
                _normalized_phone_sql(ZohoDealer.phone),
                _normalized_phone_sql(ZohoDealer.mobile),
            ).where(
                ZohoDealer.dealer_id.is_not(None),
                or_(
                    _normalized_phone_sql(ZohoDealer.phone).in_(keys),
                    _normalized_phone_sql(ZohoDealer.mobile).in_(keys),
                ),
            )
        )
    ).all():
        for p10 in (phone_p10, mobile_p10):
            if p10 and str(p10) in keys:
                dealer_keys.add(str(p10))

    for cid, key in pending.items():
        if key in dealer_keys:
            kinds[cid] = "dealer"

    lead_lookup = {k for cid, k in pending.items() if cid not in kinds}
    if lead_lookup:
        lead_keys: set[str] = set()
        for (p10,) in (
            await db.execute(
                select(_normalized_phone_sql(Lead.phone)).where(
                    _normalized_phone_sql(Lead.phone).in_(list(lead_lookup))
                )
            )
        ).all():
            if p10:
                lead_keys.add(str(p10))
        for cid, key in pending.items():
            if cid not in kinds and key in lead_keys:
                kinds[cid] = "lead"

    return kinds


async def _resolve_conversation_dealer(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    persist: bool = True,
) -> Dealer | None:
    """Return linked dealer; if unset, try phone match and optionally persist the link."""
    dealer: Dealer | None = None
    if conv.dealer_id:
        dealer = await db.get(Dealer, conv.dealer_id)
        if dealer:
            if persist and (conv.customer_type or "").lower() != "dealer":
                conv.customer_type = "Dealer"
            return dealer
    matched = await _find_dealer_by_phone(db, conv.customer_phone)
    if matched and persist:
        conv.dealer_id = matched.id
        conv.customer_type = "Dealer"
        await db.flush()
    return matched


async def _dealers_map(db: AsyncSession, dealer_ids: set[int]) -> dict[int, Dealer]:
    if not dealer_ids:
        return {}
    rows = (await db.execute(select(Dealer).where(Dealer.id.in_(dealer_ids)))).scalars().all()
    return {d.id: d for d in rows}


def _msg_out(msg: WhatsAppMessage) -> dict[str, Any]:
    media = msg.media_url or ""
    # Never return raw data:/base64 blobs to the client — only public paths or http(s)
    if media.startswith("data:"):
        media = ""
    elif len(media) > 2000 and not media.startswith(("http://", "https://", PUBLIC_PREFIX)):
        media = ""
    media_type = (msg.media_type or "").strip().lower()
    # Never expose raw "[media]" placeholder to the UI when we know the type.
    body = msg.body or ""
    if body.strip() in {"[media]", "media"} and media_type:
        body = {
            "image": "📷 Photo",
            "video": "🎥 Video",
            "audio": "🎤 Voice message",
            "ptt": "🎤 Voice message",
            "voice": "🎤 Voice message",
            "document": "📄 Document",
            "sticker": "Sticker",
        }.get(media_type, "Attachment")
    elif body.strip() in {"[media]", "media"} and not media and not media_type:
        body = "Attachment"
    return {
        "id": msg.public_id,
        "direction": msg.direction,
        "body": body,
        "media_url": media,
        "media_type": msg.media_type,
        "media_name": msg.media_name,
        "status": msg.status,
        "sender_name": msg.sender_name,
        "created_at": msg.created_at,
        "external_message_id": msg.external_message_id,
        "is_voice": media_type in {"audio", "ptt", "voice"},
        "transcript": getattr(msg, "transcript", None) or "",
        "transcript_language": getattr(msg, "transcript_language", None) or "",
        "spoken_language": getattr(msg, "spoken_language", None) or "",
        "transcription_status": getattr(msg, "transcription_status", None) or "",
        "transcription_error": getattr(msg, "transcription_error", None) or "",
    }


class NewChatIn(BaseModel):
    instance_id: str
    customer_phone: str
    customer_name: str = ""
    message: str = ""


class SendMessageIn(BaseModel):
    body: str = ""
    client_message_id: str = ""
    media_url: str = ""
    media_type: str = ""
    media_name: str = ""


class NoteIn(BaseModel):
    body: str = Field(min_length=1)


class AssignIn(BaseModel):
    employee_id: int | None = None
    assign_to_me: bool = False


class ResolveIn(BaseModel):
    reason: str = ""


class MarkSpamIn(BaseModel):
    confirmed: bool = False


class ContactUpdateIn(BaseModel):
    customer_name: str | None = None
    region: str | None = None
    language: str | None = None
    customer_type: str | None = None


class LinkDealerIn(BaseModel):
    dealer_id: int


@router.get("/api/whatsapp/inbox/campaign-reply-filters")
async def inbox_campaign_reply_filters(db: Db, user: CurrentUser):
    """Campaign ID pills for Inbox: runs with replies (same replied logic as Report)."""
    _ = user
    from app.campaign_reports import list_campaign_reply_filters, refresh_campaign_run_replied_count

    # Light refresh for live / recent runs so badge counts stay useful.
    live_rows = (
        await db.execute(
            select(CampaignRun)
            .where(CampaignRun.uid.is_not(None))
            .where(
                or_(
                    CampaignRun.status.in_(("running", "queued")),
                    CampaignRun.replied_count > 0,
                    CampaignRun.success_count > 0,
                )
            )
            .order_by(CampaignRun.uid.desc())
            .limit(12)
        )
    ).scalars().all()
    refreshed = 0
    for run in live_rows:
        if refreshed >= 6:
            break
        try:
            await refresh_campaign_run_replied_count(db, run)
            refreshed += 1
        except Exception:  # noqa: BLE001
            continue
    if refreshed:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

    items = await list_campaign_reply_filters(db)
    return {"items": items}


@router.get("/api/whatsapp/reply-stats")
async def whatsapp_reply_stats(
    db: Db,
    user: CurrentUser,
    period: str = Query("7d", pattern="^(today|7d|30d|all)$"),
):
    """Average / median staff reply latency for appreciation dashboards."""
    from app.whatsapp_reply_latency import aggregate_reply_stats

    allowed = await _authorized_instance_ids(db, user)
    allowed_set = None if allowed is None else set(allowed)
    return await aggregate_reply_stats(db, period=period, allowed_instance_ids=allowed_set)


@router.get("/api/whatsapp/reply-analytics")
async def whatsapp_reply_analytics(
    db: Db,
    user: CurrentUser,
    period: str = Query("7d", pattern="^(today|7d|30d|all)$"),
):
    """Staff performance metrics for Reply stats page (no message bodies / AI chat logs)."""
    from app.whatsapp_reply_analytics import aggregate_reply_analytics

    allowed = await _authorized_instance_ids(db, user)
    allowed_set = None if allowed is None else set(allowed)
    return await aggregate_reply_analytics(db, period=period, allowed_instance_ids=allowed_set)


@router.get("/api/whatsapp/inbox/accounts")
async def inbox_accounts(
    db: Db,
    user: CurrentUser,
    lite: bool = Query(False, description="Skip 7d reply-latency aggregation for faster inbox paint"),
):
    allowed = await _authorized_instance_ids(db, user)
    q = select(WhatsAppInstance).where(
        WhatsAppInstance.organization_id == ORG,
        WhatsAppInstance.deleted_at.is_(None),
    )
    if allowed is not None:
        if not allowed:
            return {"items": []}
        q = q.where(WhatsAppInstance.id.in_(allowed))
    rows = (await db.execute(q.order_by(WhatsAppInstance.display_name))).scalars().all()
    unreplied_by_inst: dict[int, int] = {}
    if rows:
        inst_pks = [r.id for r in rows]
        unreplied_rows = (
            await db.execute(
                select(
                    WhatsAppConversation.whatsapp_instance_id,
                    func.count(),
                )
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.whatsapp_instance_id.in_(inst_pks),
                    newsletter_list_filter(),
                    dm_inbox_filter(),
                    _not_spam_filter(),
                    WhatsAppConversation.awaiting_staff_reply.is_(True),
                    WhatsAppConversation.status != "Resolved",
                )
                .group_by(WhatsAppConversation.whatsapp_instance_id)
            )
        ).all()
        unreplied_by_inst = {int(iid): int(n or 0) for iid, n in unreplied_rows}
    reply_by_inst: dict[int, dict[str, Any]] = {}
    if not lite:
        try:
            from app.whatsapp_reply_latency import aggregate_reply_stats

            allowed = await _authorized_instance_ids(db, user)
            stats = await aggregate_reply_stats(db, period="7d", allowed_instance_ids=allowed)
            for it in stats.get("items") or []:
                iid = int(it.get("whatsapp_instance_id") or 0)
                if iid:
                    reply_by_inst[iid] = it
        except Exception:  # noqa: BLE001
            reply_by_inst = {}
    return {
        "items": [
            {
                "id": r.public_id,
                "pk": r.id,
                "display_name": r.display_name,
                "connection_status": r.connection_status,
                "connected_phone_number": r.connected_phone_number,
                "purpose": r.purpose,
                "provider": getattr(r, "provider", None) or "evolution",
                "unread_count": unreplied_by_inst.get(r.id, 0),
                "unreplied_count": unreplied_by_inst.get(r.id, 0),
                "reply_avg_label": (reply_by_inst.get(r.id) or {}).get("avg_label"),
                "reply_office_avg_label": (reply_by_inst.get(r.id) or {}).get("office_avg_label"),
                "reply_off_avg_label": (reply_by_inst.get(r.id) or {}).get("off_avg_label"),
                "reply_count_7d": (reply_by_inst.get(r.id) or {}).get("count"),
                "sarvam_voice_id": getattr(r, "sarvam_voice_id", "") or "",
                "voice_tts_enabled": bool(getattr(r, "voice_tts_enabled", False)),
            }
            for r in rows
        ]
    }


@router.get("/api/whatsapp/conversations")
async def list_conversations(
    db: Db,
    user: CurrentUser,
    tab: str = Query("All"),
    q: str = Query(""),
    instance_id: str = Query(""),
    agent: str = Query("all"),
    status: str = Query(""),
    sort: str = Query("recent"),
    campaign_uid: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service_category: str = Query(""),
    service_priority: str = Query(""),
    include_metrics: bool = Query(True, description="Inbox badge counts — skip for fast list-only loads"),
    include_total: bool = Query(True, description="Filtered thread total on first page"),
    lite: bool = Query(False, description="Skip dealer lifetime sales on rows"),
    metrics_only: bool = Query(False, description="Return metrics only (parallel with list fetch)"),
    hide_groups: bool = Query(
        True,
        description="Exclude @g.us group chats from list + badge/category counts (Groups tab still shows groups)",
    ),
):
    if offset > 0:
        include_metrics = False
        include_total = False
    allowed = await _authorized_instance_ids(db, user)
    if allowed is not None and not allowed:
        return {"items": [], "total": 0, "metrics": _empty_metrics()}

    # Campaign ID filter: only conversations that replied after that campaign's send.
    campaign_conv_ids: set[int] | None = None
    if campaign_uid is not None:
        from app.campaign_reports import (
            find_campaign_run_by_uid,
            list_campaign_run_replied_conversation_ids,
            refresh_campaign_run_replied_count,
        )

        run = await find_campaign_run_by_uid(db, int(campaign_uid))
        if not run:
            return {"items": [], "total": 0, "metrics": _empty_metrics()}
        try:
            await refresh_campaign_run_replied_count(db, run)
            await db.flush()
        except Exception:  # noqa: BLE001
            pass
        campaign_conv_ids = await list_campaign_run_replied_conversation_ids(db, run)
        if not campaign_conv_ids:
            return {"items": [], "total": 0, "metrics": _empty_metrics()}

    # Do NOT apply ``_lid_orphan_hidden_filter`` here — correlated EXISTS + ORDER BY
    # forces a multi-second nested loop even with LIMIT. Drop orphans after fetch.
    # Groups tab intentionally shows @g.us even when hide_groups is on.
    apply_hide_groups = bool(hide_groups) and (tab or "").strip().lower() != "groups"
    stmt = select(WhatsAppConversation).where(
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        _not_spam_filter(),
        *((dm_inbox_filter(),) if apply_hide_groups else ()),
    )
    if allowed is not None:
        stmt = stmt.where(WhatsAppConversation.whatsapp_instance_id.in_(allowed))
    if campaign_conv_ids is not None:
        stmt = stmt.where(WhatsAppConversation.id.in_(campaign_conv_ids))

    metrics_instance_id: int | None = None
    if instance_id:
        inst = await _owned_instance(db, instance_id) if _is_admin(user) else (
            await db.execute(
                select(WhatsAppInstance).where(
                    WhatsAppInstance.public_id == instance_id,
                    WhatsAppInstance.organization_id == ORG,
                    WhatsAppInstance.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not inst:
            raise HTTPException(404, "Instance not found")
        if allowed is not None and inst.id not in allowed:
            raise HTTPException(403, "Not authorized for this instance")
        metrics_instance_id = inst.id
        stmt = stmt.where(WhatsAppConversation.whatsapp_instance_id == inst.id)

    tab_l = tab.lower()
    if tab_l == "mine":
        stmt = stmt.where(
            or_(
                WhatsAppConversation.assigned_user_id == user.id,
                WhatsAppConversation.assigned_employee_id.in_(
                    select(Employee.id).where(or_(Employee.user_id == user.id, Employee.email == user.email))
                ),
            )
        )
        stmt = stmt.where(WhatsAppConversation.status != "Resolved")
    elif tab_l == "unassigned":
        stmt = stmt.where(
            WhatsAppConversation.assigned_employee_id.is_(None),
            WhatsAppConversation.assigned_user_id.is_(None),
            WhatsAppConversation.status != "Resolved",
        )
    elif tab_l == "unread":
        stmt = stmt.where(WhatsAppConversation.unread_count > 0)
    elif tab_l == "resolved":
        stmt = stmt.where(WhatsAppConversation.status == "Resolved")
    elif tab_l == "groups":
        # Named WhatsApp groups only — hide raw-id shells / channels.
        stmt = stmt.where(_named_group_filter())

    if status:
        stmt = stmt.where(WhatsAppConversation.status == status)
    if agent == "none":
        stmt = stmt.where(WhatsAppConversation.assigned_employee_id.is_(None))
    elif agent not in {"", "all"} and agent.isdigit():
        stmt = stmt.where(WhatsAppConversation.assigned_employee_id == int(agent))

    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                WhatsAppConversation.customer_name.ilike(like),
                WhatsAppConversation.customer_phone.ilike(like),
                WhatsAppConversation.last_message_preview.ilike(like),
                WhatsAppConversation.tag.ilike(like),
                WhatsAppConversation.customer_jid.ilike(like),
            )
        )

    sp = (service_priority or "").strip().title()
    if sp in {"Med", "Mid"}:
        sp = "Medium"
    if sp in {"High", "Medium", "Low"}:
        stmt = stmt.where(func.lower(WhatsAppConversation.priority) == sp.lower())

    # Chip counts: same tab / account / search / priority scope, never the active category chip.
    list_scope = stmt.whereclause

    sc = (service_category or "").strip().lower()
    if sc == "sales":
        stmt = stmt.where(
            func.lower(WhatsAppConversation.service_category).in_(["sales", "support"])
        )
    elif sc == "service":
        stmt = stmt.where(func.lower(WhatsAppConversation.service_category) == "service")
    elif sc == "cloud":
        stmt = stmt.where(func.lower(WhatsAppConversation.service_category) == "cloud")
    elif sc == "stamping":
        stmt = stmt.where(func.lower(WhatsAppConversation.service_category) == "stamping")

    if metrics_only:
        metrics, cats = await asyncio.gather(
            _metrics(db, allowed, instance_id=metrics_instance_id, hide_groups=apply_hide_groups),
            _service_category_counts(db, list_scope),
        )
        metrics["service_categories"] = cats
        return {"items": [], "has_more": False, "metrics": metrics}

    activity = _activity_at()
    if sort == "oldest":
        stmt = stmt.order_by(activity.asc().nullslast())
    elif sort == "unread":
        stmt = stmt.order_by(WhatsAppConversation.unread_count.desc(), activity.desc().nullslast())
    else:
        # Default "Most recent": unread threads first, then recency within each group.
        stmt = stmt.order_by(
            desc(WhatsAppConversation.unread_count > 0),
            activity.desc().nullslast(),
        )

    # Over-fetch then drop @lid orphans in Python so page still fills to ``limit``.
    fetch_limit = min(max(limit + 40, limit), 200)
    raw_rows = (await db.execute(stmt.offset(offset).limit(fetch_limit))).scalars().all()
    filtered = await _drop_lid_orphans(db, raw_rows)
    rows = filtered[:limit]
    has_more = len(filtered) > limit
    total: int | None = None
    if offset == 0 and include_total:
        total = int(
            (await db.execute(select(func.count()).select_from(WhatsAppConversation).where(stmt.whereclause))).scalar()
            or 0
        )

    inst_ids = {r.whatsapp_instance_id for r in rows}
    emp_ids = {r.assigned_employee_id for r in rows if r.assigned_employee_id}
    dealer_ids = {r.dealer_id for r in rows if getattr(r, "dealer_id", None)}
    inst_name: dict[int, str] = {}
    inst_provider: dict[int, str] = {}
    if inst_ids:
        for i in (
            await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.id.in_(inst_ids)))
        ).scalars().all():
            inst_name[i.id] = i.display_name
            inst_provider[i.id] = (getattr(i, "provider", None) or "evolution").strip().lower()
    emp_map = {}
    if emp_ids:
        for e in (await db.execute(select(Employee).where(Employee.id.in_(emp_ids)))).scalars().all():
            emp_map[e.id] = e.name
    dealer_map = await _dealers_map(db, dealer_ids)
    crm_kind_map = await _crm_contact_kinds_map(db, rows, dealer_map=dealer_map)

    care_conv_ids = [r.id for r in rows if inst_provider.get(r.whatsapp_instance_id) == "meta_cloud"]
    inbound_map = await _last_inbound_at_map(db, care_conv_ids)

    dealer_sales_map: dict[int, float] = {}
    if not lite and dealer_map:
        dealer_sales_map = await dealer_lifetime_sales_map(db, list(dealer_map.values()))

    items = [
        _conv_out(
            r,
            account=inst_name.get(r.whatsapp_instance_id, ""),
            agent=emp_map.get(r.assigned_employee_id or 0, ""),
            dealer=dealer_map.get(r.dealer_id) if getattr(r, "dealer_id", None) else None,
            dealer_sales_total=(
                dealer_sales_map.get(int(r.dealer_id))
                if (not lite and getattr(r, "dealer_id", None))
                else None
            ),
            provider=inst_provider.get(r.whatsapp_instance_id, ""),
            last_inbound_at=inbound_map.get(r.id),
            crm_contact_kind=crm_kind_map.get(r.id),
        )
        for r in rows
    ]
    items = await enrich_conversation_rows(db, rows, items)
    out: dict[str, Any] = {"items": items, "has_more": has_more}
    if include_metrics:
        metrics, cats = await asyncio.gather(
            _metrics(db, allowed, instance_id=metrics_instance_id, hide_groups=apply_hide_groups),
            _service_category_counts(db, list_scope),
        )
        metrics["service_categories"] = cats
        out["metrics"] = metrics
    if total is not None:
        out["total"] = total
    return out


def _empty_metrics() -> dict[str, Any]:
    return {
        "open": 0,
        "unread": 0,
        "unread_conversations": 0,
        "unreplied": 0,
        "unassigned": 0,
        "agents_online": 0,
        "agents_total": 0,
        "service_categories": _empty_service_category_counts(),
    }


def _empty_service_category_counts() -> dict[str, int]:
    return {"all": 0, "sales": 0, "service": 0, "cloud": 0, "stamping": 0}


async def _service_category_counts(db: AsyncSession, list_scope) -> dict[str, int]:
    """Per-category conversation totals for the current inbox list scope (excludes category chip)."""

    def _count_stmt(*extra):
        parts: list[Any] = []
        if list_scope is not None:
            parts.append(list_scope)
        parts.extend(extra)
        return select(func.count()).select_from(WhatsAppConversation).where(*parts)

    all_n = (await db.execute(_count_stmt())).scalar() or 0
    sales_n = (
        await db.execute(
            _count_stmt(func.lower(WhatsAppConversation.service_category).in_(["sales", "support"]))
        )
    ).scalar() or 0
    service_n = (
        await db.execute(
            _count_stmt(func.lower(WhatsAppConversation.service_category) == "service")
        )
    ).scalar() or 0
    cloud_n = (
        await db.execute(
            _count_stmt(func.lower(WhatsAppConversation.service_category) == "cloud")
        )
    ).scalar() or 0
    stamp_n = (
        await db.execute(
            _count_stmt(func.lower(WhatsAppConversation.service_category) == "stamping")
        )
    ).scalar() or 0
    return {
        "all": int(all_n),
        "sales": int(sales_n),
        "service": int(service_n),
        "cloud": int(cloud_n),
        "stamping": int(stamp_n),
    }


async def _metrics(
    db: AsyncSession,
    allowed: list[int] | None,
    *,
    instance_id: int | None = None,
    hide_groups: bool = True,
) -> dict[str, Any]:
    """Inbox badge counts.

    Intentionally omits ``_lid_orphan_hidden_filter`` — that correlated EXISTS
    (regexp + twin scan) turns each COUNT into a multi-second table scan on
    ~8k rows and blocked the whole conversations list for ~20–35s. Lid orphans
    are still hidden in the list query (LIMIT makes that cheap); badge drift
    of a few rows is acceptable.
    """
    if allowed is not None and not allowed:
        return _empty_metrics()

    scope = [
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        _not_spam_filter(),
    ]
    if hide_groups:
        scope.append(dm_inbox_filter())
    if allowed is not None:
        scope.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed))
    if instance_id is not None:
        scope.append(WhatsAppConversation.whatsapp_instance_id == instance_id)

    open_n = (
        await db.execute(
            select(func.count())
            .select_from(WhatsAppConversation)
            .where(*scope, WhatsAppConversation.status != "Resolved")
        )
    ).scalar() or 0
    unread_n = (
        await db.execute(
            select(func.coalesce(func.sum(WhatsAppConversation.unread_count), 0)).where(*scope)
        )
    ).scalar() or 0
    unreplied_n = (
        await db.execute(
            select(func.count())
            .select_from(WhatsAppConversation)
            .where(
                *scope,
                dm_inbox_filter(),
                WhatsAppConversation.awaiting_staff_reply.is_(True),
                WhatsAppConversation.status != "Resolved",
            )
        )
    ).scalar() or 0
    unassigned_n = (
        await db.execute(
            select(func.count())
            .select_from(WhatsAppConversation)
            .where(
                *scope,
                WhatsAppConversation.status != "Resolved",
                WhatsAppConversation.assigned_employee_id.is_(None),
                WhatsAppConversation.assigned_user_id.is_(None),
            )
        )
    ).scalar() or 0
    unread_convs = (
        await db.execute(
            select(func.count())
            .select_from(WhatsAppConversation)
            .where(*scope, WhatsAppConversation.unread_count > 0)
        )
    ).scalar() or 0
    agents_total = (
        await db.execute(
            select(func.count()).select_from(Employee).where(Employee.status.in_(["Active", "Probation"]))
        )
    ).scalar() or 0
    return {
        "open": open_n,
        "unread": int(unread_n),  # sum of unread inbound messages from phone (incl. groups)
        "unreplied": int(unreplied_n),  # 1:1 threads still needing a human reply (@g.us excluded)
        "unread_conversations": int(unread_convs),
        "unassigned": unassigned_n,
        "agents_online": agents_total,
        "agents_total": agents_total,
    }


@router.get("/api/whatsapp/conversations/{public_id}")
async def get_conversation(public_id: str, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    dealer = await _resolve_conversation_dealer(db, conv, persist=True)
    if dealer or conv.dealer_id:
        await db.commit()
        await db.refresh(conv)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    agent = ""
    if conv.assigned_employee_id:
        emp = await db.get(Employee, conv.assigned_employee_id)
        agent = emp.name if emp else ""
    out = await _conv_out_with_instance(db, conv, agent=agent, dealer=dealer, inst=inst)
    out["ai_outline"] = getattr(conv, "ai_outline", None) or ""
    out["ai_outline_at"] = getattr(conv, "ai_outline_at", None)
    holds = await hold_since_map(db, [conv])
    summary = parse_cached_outline_summary(getattr(conv, "ai_outline", None) or "")
    out.update(resolve_service_fields(conv, summary, hold_since=holds.get(conv.id)))
    try:
        from app.whatsapp_new_messenger import load_onboarding_for_conv

        onboarding = await load_onboarding_for_conv(db, conv)
        if onboarding:
            out["onboarding"] = onboarding
    except Exception:  # noqa: BLE001
        pass
    return out


@router.patch("/api/whatsapp/conversations/{public_id}/contact")
async def update_contact(public_id: str, body: ContactUpdateIn, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    if body.customer_name is not None:
        conv.customer_name = body.customer_name.strip()[:160]
    if body.region is not None:
        conv.region = body.region.strip()[:120]
    if body.language is not None:
        conv.language = body.language.strip()[:40]
    if body.customer_type is not None and body.customer_type.strip():
        # Keep Dealer when linked; otherwise allow Customer/Lead/etc.
        if conv.dealer_id and body.customer_type.strip().lower() != "dealer":
            pass
        else:
            conv.customer_type = body.customer_type.strip()[:40]
    await db.commit()
    await db.refresh(conv)
    dealer = await db.get(Dealer, conv.dealer_id) if conv.dealer_id else None
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    agent = ""
    if conv.assigned_employee_id:
        emp = await db.get(Employee, conv.assigned_employee_id)
        agent = emp.name if emp else ""
    return await _conv_out_with_instance(db, conv, agent=agent, dealer=dealer, inst=inst)


def _dealer_link_search_item(d: Dealer) -> dict[str, Any]:
    return {
        "id": d.id,
        "name": d.name or "",
        "company": d.company or "",
        "phone": d.phone or "",
        "code": d.zoho_contact_id or "",
        "city": d.city or "",
        "state": d.state or "",
    }


@router.get("/api/whatsapp/conversations/{public_id}/dealers/search")
async def search_dealers_for_conversation(
    public_id: str,
    db: Db,
    user: CurrentUser,
    q: str = Query(""),
    limit: int = Query(20, ge=1, le=40),
):
    """Fuzzy name/company search plus phone and Zoho code matches for linking a dealer."""
    from app.ai.tools import _search_dealer_rows
    from app.dealer_exclusions import dealer_not_excluded_clause

    await _get_conversation(db, public_id, user)
    term = q.strip()
    if not term:
        return {"items": []}

    seen: dict[int, Dealer] = {}
    order: list[int] = []

    for dealer, _score in await _search_dealer_rows(db, term, limit=limit):
        if dealer.id not in seen:
            seen[dealer.id] = dealer
            order.append(dealer.id)

    if len(order) < limit:
        like = f"%{term}%"
        phone_key = normalize_in_mobile(term)
        phone_clauses = [
            Dealer.phone.ilike(like),
            Dealer.zoho_contact_id.ilike(like),
        ]
        if phone_key:
            phone_clauses.append(_normalized_phone_sql(Dealer.phone) == phone_key)
            tail = phone_key[-10:] if len(phone_key) >= 10 else phone_key
            if tail:
                phone_clauses.append(_normalized_phone_sql(Dealer.phone).like(f"%{tail}%"))
        visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
        extra = (
            await db.execute(
                select(Dealer)
                .where(visible)
                .where(or_(*phone_clauses))
                .order_by(Dealer.name.asc())
                .limit(limit)
            )
        ).scalars().all()
        for d in extra:
            if d.id not in seen:
                seen[d.id] = d
                order.append(d.id)
            if len(order) >= limit:
                break

    rows = [seen[i] for i in order[:limit]]
    return {"items": [_dealer_link_search_item(d) for d in rows]}


@router.post("/api/whatsapp/conversations/{public_id}/link-dealer")
async def link_dealer(public_id: str, body: LinkDealerIn, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    dealer = await db.get(Dealer, body.dealer_id)
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    conv.dealer_id = dealer.id
    conv.customer_type = "Dealer"
    await db.commit()
    await db.refresh(conv)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    agent = ""
    if conv.assigned_employee_id:
        emp = await db.get(Employee, conv.assigned_employee_id)
        agent = emp.name if emp else ""
    return await _conv_out_with_instance(db, conv, agent=agent, dealer=dealer, inst=inst)


@router.post("/api/whatsapp/conversations/{public_id}/unlink-dealer")
async def unlink_dealer(public_id: str, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    if not conv.dealer_id:
        raise HTTPException(400, "No dealer linked")
    conv.dealer_id = None
    if (conv.customer_type or "").strip().casefold() == "dealer":
        conv.customer_type = "Customer"
    await db.commit()
    await db.refresh(conv)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    agent = ""
    if conv.assigned_employee_id:
        emp = await db.get(Employee, conv.assigned_employee_id)
        agent = emp.name if emp else ""
    return await _conv_out_with_instance(db, conv, agent=agent, dealer=None, inst=inst)


@router.get("/api/whatsapp/conversations/{public_id}/outline")
async def get_outline(public_id: str, db: Db, user: CurrentUser):
    """Return cached structured AI summary when still fresh for the latest messages."""
    from app.ai.service import outline_lacks_malayalam_for_ml, normalize_outline_summary
    from app.whatsapp_outline import (
        cached_summary_payload,
        load_recent_messages,
        outline_fingerprint,
        preferred_outline_language,
    )

    conv = await _get_conversation(db, public_id, user)
    messages = await load_recent_messages(db, conv)
    preferred_language = await preferred_outline_language(db, conv)
    fp = outline_fingerprint(messages, preferred_language=preferred_language)
    cached = (getattr(conv, "ai_outline", None) or "").strip()
    cached_fp = getattr(conv, "ai_outline_fingerprint", None) or ""
    # Legacy plain-text cache is treated as stale so structured JSON is generated once.
    legacy = bool(cached) and not cached.lstrip().startswith("{")
    wrong_ml = False
    if preferred_language == "ml" and cached:
        wrong_ml = outline_lacks_malayalam_for_ml(normalize_outline_summary(cached)) or (
            outline_lacks_malayalam_for_ml(cached)
        )
    stale = not cached or cached_fp != fp or legacy or wrong_ml
    payload = cached_summary_payload(conv)
    return {
        **payload,
        "stale": stale,
        "fingerprint": fp,
        "preferred_language": preferred_language,
        "cached": bool(cached) and not stale,
    }


@router.post("/api/whatsapp/conversations/{public_id}/outline")
async def generate_outline(
    public_id: str,
    db: Db,
    user: CurrentUser,
    force: bool = Query(False),
):
    """Generate (or return cached) Gemini structured summary from text + voice STT."""
    from app.whatsapp_outline import refresh_conversation_outline

    conv = await _get_conversation(db, public_id, user)
    result = await refresh_conversation_outline(db, conv, user=user, force=force)
    if result.get("ok"):
        if not result.get("skipped"):
            try:
                await db.commit()
                await db.refresh(conv)
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                raise HTTPException(502, f"Could not save outline: {exc}") from exc
        return {
            "outline": result.get("outline") or "",
            "summary": result.get("summary") or {},
            "stale": bool(result.get("stale")),
            "fingerprint": result.get("fingerprint"),
            "preferred_language": result.get("preferred_language"),
            "generated_at": result.get("generated_at") or getattr(conv, "ai_outline_at", None),
            "last_message_id": result.get("last_message_id"),
            "cached": bool(result.get("cached")),
            "model": result.get("model"),
            "usage": result.get("usage"),
            **({"error": result["error"]} if result.get("error") else {}),
        }
    err = result.get("error") or "Outline failed"
    if result.get("budget"):
        raise HTTPException(429, err)
    if "No text or voice transcripts" in err:
        raise HTTPException(400, err)
    if result.get("cached"):
        return {
            "outline": result.get("outline") or "",
            "summary": result.get("summary") or {},
            "stale": True,
            "fingerprint": result.get("fingerprint"),
            "preferred_language": result.get("preferred_language"),
            "generated_at": result.get("generated_at"),
            "last_message_id": result.get("last_message_id"),
            "cached": True,
            "model": None,
            "error": err,
        }
    if "Not configured" in err or "disabled" in err.lower():
        raise HTTPException(503, err)
    raise HTTPException(502, err)


@router.get("/api/whatsapp/conversations/{public_id}/related")
async def related_records(public_id: str, db: Db, user: CurrentUser):
    """Related CRM records for the right-panel Related tab (best-effort)."""
    conv = await _get_conversation(db, public_id, user)
    phone = _phone_digits(conv.customer_phone)
    tail = phone[-10:] if len(phone) >= 10 else phone

    dealer = None
    if conv.dealer_id:
        d = await db.get(Dealer, conv.dealer_id)
        if d:
            dealer = {
                "id": d.id,
                "name": d.name,
                "company": getattr(d, "company", "") or "",
                "phone": d.phone,
                "code": getattr(d, "code", "") or "",
                "city": getattr(d, "city", "") or "",
            }

    leads: list[dict[str, Any]] = []
    if tail:
        lead_rows = (
            await db.execute(
                select(Lead)
                .where(Lead.phone.ilike(f"%{tail}%"))
                .order_by(Lead.id.desc())
                .limit(12)
            )
        ).scalars().all()
        leads = [
            {
                "id": r.id,
                "name": r.name,
                "phone": r.phone,
                "status": r.status,
                "company": r.company,
                "created_at": r.created_at,
            }
            for r in lead_rows
        ]

    tasks: list[dict[str, Any]] = []
    try:
        # Open work items mentioning this phone/name — soft match on title/description.
        needle = (conv.customer_name or "").strip() or tail
        task_q = select(WorkItem).where(WorkItem.status.in_(["todo", "doing", "blocked"])).order_by(WorkItem.id.desc()).limit(40)
        task_rows = (await db.execute(task_q)).scalars().all()
        for t in task_rows:
            blob = f"{t.title} {t.description}".lower()
            if (tail and tail in blob) or (needle and needle.lower() in blob):
                tasks.append(
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "priority": t.priority,
                        "due_date": t.due_date,
                    }
                )
            if len(tasks) >= 8:
                break
    except Exception:  # noqa: BLE001
        tasks = []

    return {
        "dealer": dealer,
        "leads": leads,
        "open_tasks": tasks,
        "quotations": [],
        "sales_orders": [],
        "previous_purchases": [],
        "service_tickets": [],
        "subscription_renewals": [],
        "unavailable": [
            "quotations",
            "sales_orders",
            "previous_purchases",
            "service_tickets",
            "subscription_renewals",
        ],
    }


@router.get("/api/whatsapp/conversations/{public_id}/messages")
async def list_messages(
    public_id: str,
    db: Db,
    user: CurrentUser,
    limit: int = Query(80, ge=1, le=200),
    before_id: str = Query(""),
):
    conv = await _get_conversation(db, public_id, user)
    stmt = (
        select(WhatsAppMessage)
        .where(WhatsAppMessage.conversation_id == conv.id)
        .order_by(WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc())
        .limit(limit)
    )
    if before_id:
        anchor = (
            await db.execute(select(WhatsAppMessage).where(WhatsAppMessage.public_id == before_id))
        ).scalar_one_or_none()
        if anchor:
            stmt = stmt.where(
                or_(
                    WhatsAppMessage.created_at < anchor.created_at,
                    and_(WhatsAppMessage.created_at == anchor.created_at, WhatsAppMessage.id < anchor.id),
                )
            )
    rows = list(reversed((await db.execute(stmt)).scalars().all()))
    return {"items": [_msg_out(m) for m in rows], "conversation_id": public_id}


@router.post("/api/whatsapp/conversations")
async def create_conversation(body: NewChatIn, db: Db, user: CurrentUser):
    phone = _phone_digits(body.customer_phone)
    if len(phone) < 8:
        raise HTTPException(400, "Valid WhatsApp number required")
    inst = (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.public_id == body.instance_id,
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not inst:
        raise HTTPException(404, "Instance not found")
    allowed = await _authorized_instance_ids(db, user)
    if allowed is not None and inst.id not in allowed:
        raise HTTPException(403, "Not authorized for this WhatsApp account")

    existing = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                WhatsAppConversation.customer_phone == phone,
            )
        )
    ).scalar_one_or_none()
    emp = await _employee_for_user(db, user)
    owner = await primary_owner_employee(db, inst.id)
    owner_name = owner.name if owner else (emp.name if emp else user.name)
    if existing:
        conv = existing
        if not conv.assigned_employee_id:
            await assign_conversation_to_instance_owner(db, conv, inst.id)
    else:
        conv = WhatsAppConversation(
            public_id=str(uuid.uuid4()),
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            customer_phone=phone,
            customer_name=body.customer_name.strip(),
            customer_jid=f"{phone}@s.whatsapp.net",
            status="Open",
            tag="New",
            assigned_user_id=owner.user_id if owner and owner.user_id else user.id,
            assigned_employee_id=owner.id if owner else (emp.id if emp else None),
        )
        db.add(conv)
        await db.flush()

    if body.message.strip():
        await _send_and_store(db, user, conv, inst, body.message.strip())

    await db.commit()
    await db.refresh(conv)
    return _conv_out(conv, account=inst.display_name, agent=owner_name)


@router.post("/api/whatsapp/conversations/{public_id}/messages")
async def send_message(public_id: str, body: SendMessageIn, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    if conv.status == "Resolved":
        raise HTTPException(400, "Conversation is resolved — reopen to reply")
    text = (body.body or "").strip()
    if not text and not body.media_url:
        raise HTTPException(400, "Message body required")
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    if not inst or inst.deleted_at:
        raise HTTPException(404, "WhatsApp instance missing")
    if inst.connection_status != "Connected":
        raise HTTPException(409, f"WhatsApp instance is {inst.connection_status}")
    from app.cloud_whatsapp import care_customer_care_window, is_meta_cloud_instance

    # Care-only: block free-form when CRM last inbound is outside Meta's 24h window.
    if is_meta_cloud_instance(inst):
        window = care_customer_care_window(await _last_inbound_at(db, conv.id))
        if not window.get("can_freeform_reply"):
            raise HTTPException(
                409,
                "Care 24h window expired — free-form reply blocked. "
                "Wait for a customer message or send a Meta template.",
            )
    msg = await _send_and_store(
        db,
        user,
        conv,
        inst,
        text,
        client_message_id=body.client_message_id,
        media_url=body.media_url,
        media_type=body.media_type,
        media_name=body.media_name,
    )
    await db.commit()
    await db.refresh(msg)
    return _msg_out(msg)


async def _send_and_store(
    db: AsyncSession,
    user: User,
    conv: WhatsAppConversation,
    inst: WhatsAppInstance,
    text: str,
    *,
    client_message_id: str = "",
    media_url: str = "",
    media_type: str = "",
    media_name: str = "",
    evo_media_b64: str | None = None,
) -> WhatsAppMessage:
    from app.cloud_whatsapp import is_meta_cloud_instance, send_care_cloud_text

    if client_message_id:
        dup = (
            await db.execute(
                select(WhatsAppMessage).where(WhatsAppMessage.client_message_id == client_message_id)
            )
        ).scalar_one_or_none()
        if dup:
            return dup

    emp = await _employee_for_user(db, user)
    care = is_meta_cloud_instance(inst)

    # Persist media on VPS disk; Postgres keeps only the public path + metadata
    stored_media_url = media_url or ""
    evo_media_payload = media_url or ""
    if (media_url or evo_media_b64) and media_type:
        if care:
            raise HTTPException(400, "Care Cloud WhatsApp replies support text only for now")
        try:
            # AI Agent / batch can pass pre-encoded Evolution payload to avoid re-reading
            # large campaign videos for every recipient.
            if evo_media_b64:
                evo_media_payload = evo_media_b64
                if media_url.startswith(PUBLIC_PREFIX):
                    stored_media_url = media_url
                elif not stored_media_url:
                    stored_media_url = media_url or ""
            elif media_url.startswith("data:") or (
                not media_url.startswith(("http://", "https://", PUBLIC_PREFIX)) and len(media_url) > 200
            ):
                public_url, _, raw = save_data_url(media_url, media_type=media_type, filename=media_name)
                stored_media_url = public_url
                evo_media_payload = base64.b64encode(raw).decode("ascii")
            elif media_url.startswith(PUBLIC_PREFIX):
                stored_media_url = media_url
                name = media_url.rsplit("/", 1)[-1]
                path = resolve_path(name)
                if path:
                    evo_media_payload = base64.b64encode(path.read_bytes()).decode("ascii")
                else:
                    evo_media_payload = to_evolution_base64(media_url)
            else:
                evo_media_payload = to_evolution_base64(media_url)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Invalid media: {exc}") from exc

    msg = WhatsAppMessage(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=inst.id,
        direction="out",
        body=text,
        media_url=stored_media_url[:500] if not stored_media_url.startswith(PUBLIC_PREFIX) else stored_media_url,
        media_type=media_type,
        media_name=media_name,
        client_message_id=client_message_id or str(uuid.uuid4()),
        status="sending",
        sender_user_id=user.id,
        sender_employee_id=emp.id if emp else None,
        sender_name=(emp.name if emp else user.name),
    )
    db.add(msg)
    await db.flush()
    # Commit BEFORE Evolution/Care so the fromMe webhook can match client_message_id
    # (or body twin) instead of inserting a second "WhatsApp" outbound row.
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.flush()

    recipient = _send_recipient(conv)
    if not recipient:
        msg.status = "failed"
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.flush()
        raise HTTPException(400, "Conversation has no valid WhatsApp recipient")
    try:
        if care:
            ext = await send_care_cloud_text(
                to_wa_id=recipient,
                text=text or media_name or "[media]",
                client_message_id=msg.client_message_id,
            )
            msg.external_message_id = ext
            msg.status = "delivered"
        else:
            _, svc = await _svc(db)
            token = decrypt_secret(inst.encrypted_instance_token)
            if evo_media_payload and media_type:
                remote = await svc.send_media(
                    token,
                    recipient,
                    media_type=media_type if media_type in {"image", "video", "audio", "document"} else "document",
                    url=evo_media_payload,
                    filename=media_name or ("voice.ogg" if media_type == "audio" else "file"),
                    caption=text,
                    msg_id=msg.client_message_id,
                )
            else:
                remote = await svc.send_text(token, recipient, text or media_name or "[media]", msg.client_message_id)
            ext = ""
            if isinstance(remote, dict):
                data = remote.get("data") if isinstance(remote.get("data"), dict) else remote
                if isinstance(data, dict):
                    key = data.get("key") if isinstance(data.get("key"), dict) else {}
                    info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
                    ext = str(data.get("id") or data.get("messageId") or key.get("id") or info.get("ID") or "")
            msg.external_message_id = ext
            # Evolution accepted the message — double gray tick (server/device ack)
            msg.status = "delivered"
    except HTTPException as http_exc:
        detail = str(getattr(http_exc, "detail", "") or http_exc)
        msg.status = "failed"
        # Keep caption if any; always surface Evolution/gateway error in chat preview
        fail_note = detail.strip() or "Send failed"
        msg.body = text.strip() if (text or "").strip() else f"[send failed] {fail_note}"[:2000]
        conv.last_message_preview = f"Failed: {fail_note}"[:255]
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.flush()
        raise
    except Exception as exc:  # noqa: BLE001
        fail_note = str(exc).strip() or "Send failed"
        msg.status = "failed"
        msg.body = text.strip() if (text or "").strip() else f"[send failed] {fail_note}"[:2000]
        conv.last_message_preview = f"Failed: {fail_note}"[:255]
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.flush()
        raise HTTPException(502, f"Send failed: {exc}") from exc
    now = datetime.now(timezone.utc)
    if media_type == "audio":
        conv.last_message_preview = "🎤 Voice message"
    elif media_type == "image":
        conv.last_message_preview = (text or "📷 Photo")[:255]
    elif media_type == "video":
        conv.last_message_preview = (text or "🎥 Video")[:255]
    elif media_type == "sticker":
        conv.last_message_preview = "Sticker"
    else:
        conv.last_message_preview = (text or media_name or "[attachment]")[:255]
    conv.last_message_at = now
    # Outbound reply from CRM = conversation is being handled → clear unread/New
    # (Opening/viewing a chat must NOT clear unread — only a successful staff reply.)
    from app.whatsapp_reply_latency import complete_staff_reply

    await complete_staff_reply(db, conv, replied_at=now, source="crm", inst=inst, msg=msg)
    await _send_whatsapp_read_receipts(db, conv, inst)
    if conv.status in {"Pending Customer Reply", "Resolved"}:
        conv.status = "Open"
    if not conv.assigned_employee_id:
        owner = await assign_conversation_to_instance_owner(db, conv, inst.id)
        if not owner and not conv.assigned_user_id:
            conv.assigned_user_id = user.id
            if emp:
                conv.assigned_employee_id = emp.id

    # Persist status / assignment updates before fire-and-forget AI work.
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.flush()

    # Auto-transcribe outbound voice/video after media is stored locally.
    if (media_type or "").lower() in {"audio", "ptt", "voice", "video"} and (
        stored_media_url or ""
    ).startswith(PUBLIC_PREFIX):
        try:
            from app.sarvam_transcribe import schedule_transcription

            if not (msg.transcription_status or "").strip():
                msg.transcription_status = "pending"
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    await db.flush()
            schedule_transcription(msg.id)
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            from app.sarvam_transcribe import is_text_translatable_message, schedule_text_translation

            if is_text_translatable_message(msg):
                if not (msg.transcription_status or "").strip():
                    msg.transcription_status = "pending"
                    try:
                        await db.commit()
                    except Exception:  # noqa: BLE001
                        await db.flush()
                schedule_text_translation(msg.id)
        except Exception:  # noqa: BLE001
            pass
    return msg


@router.post("/api/whatsapp/conversations/{public_id}/notes")
async def add_note(public_id: str, body: NoteIn, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    emp = await _employee_for_user(db, user)
    msg = WhatsAppMessage(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=conv.whatsapp_instance_id,
        direction="note",
        body=body.body.strip(),
        status="stored",
        sender_user_id=user.id,
        sender_employee_id=emp.id if emp else None,
        sender_name=(emp.name if emp else user.name),
    )
    db.add(msg)
    conv.notes_summary = body.body.strip()[:500]
    await db.commit()
    await db.refresh(msg)
    return _msg_out(msg)


async def _send_whatsapp_read_receipts(
    db: AsyncSession,
    conv: WhatsAppConversation,
    inst: WhatsAppInstance | None = None,
) -> None:
    """Best-effort WhatsApp blue-tick receipts (Evolution). Never raises."""
    try:
        if inst is None:
            inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
        recipient = _send_recipient(conv)
        if not inst or not recipient:
            return
        recent_in = (
            await db.execute(
                select(WhatsAppMessage.external_message_id)
                .where(
                    WhatsAppMessage.conversation_id == conv.id,
                    WhatsAppMessage.direction == "in",
                    WhatsAppMessage.external_message_id != "",
                )
                .order_by(WhatsAppMessage.created_at.desc())
                .limit(20)
            )
        ).scalars().all()
        ids = [mid for mid in recent_in if mid]
        if not ids:
            return
        _, svc = await _svc(db)
        token = decrypt_secret(inst.encrypted_instance_token)
        await svc.mark_read(token, recipient, ids)
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/whatsapp/conversations/{public_id}/read")
async def mark_read(public_id: str, db: Db, user: CurrentUser):
    """No-op for CRM unread/status.

    Opening a chat must not clear unread or flip New→Open. Unread clears only when
    staff successfully sends a reply (`_send_and_store` → `clear_conversation_unread`).
    Kept for backward-compatible clients; returns current conversation state.
    """
    conv = await _get_conversation(db, public_id, user)
    return {
        "ok": True,
        "unread_count": int(conv.unread_count or 0),
        "status": conv.status,
        "tag": conv.tag or ("New" if conv.status == "New" else conv.status),
    }


@router.post("/api/whatsapp/conversations/{public_id}/assign")
async def assign_conversation(public_id: str, body: AssignIn, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    if body.assign_to_me:
        emp = await _employee_for_user(db, user)
        conv.assigned_user_id = user.id
        conv.assigned_employee_id = emp.id if emp else None
    elif body.employee_id is None:
        conv.assigned_employee_id = None
        conv.assigned_user_id = None
    else:
        emp = await db.get(Employee, body.employee_id)
        if not emp:
            raise HTTPException(404, "Employee not found")
        conv.assigned_employee_id = emp.id
        conv.assigned_user_id = emp.user_id
    if conv.status == "New":
        conv.status = "Open"
    if (conv.tag or "").strip().lower() == "new":
        conv.tag = ""
    await db.commit()
    return {"ok": True, "assigned_employee_id": conv.assigned_employee_id}


@router.post("/api/whatsapp/conversations/{public_id}/resolve")
async def resolve_conversation(public_id: str, body: ResolveIn, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    conv.status = "Resolved"
    conv.tag = "Resolved"
    if body.reason:
        conv.notes_summary = (conv.notes_summary + "\n" if conv.notes_summary else "") + f"Resolved: {body.reason}"
    await db.commit()
    return {"ok": True, "status": conv.status}


@router.post("/api/whatsapp/conversations/{public_id}/spam")
async def mark_conversation_spam(public_id: str, body: MarkSpamIn, db: Db, user: CurrentUser):
    """Soft-flag as spam: hide from inbox + counts; keep messages for DB recovery."""
    if not body.confirmed:
        raise HTTPException(400, "Confirmation required — check confirm it is spam")
    conv = await _get_conversation(db, public_id, user)
    if getattr(conv, "is_spam", False):
        return {"ok": True, "is_spam": True}
    conv.is_spam = True
    conv.spam_at = datetime.now(timezone.utc)
    conv.spam_by_user_id = user.id
    conv.awaiting_staff_reply = False
    conv.awaiting_since = None
    if (conv.unread_count or 0) != 0:
        conv.unread_count = 0
    await db.commit()
    return {"ok": True, "is_spam": True}


@router.post("/api/whatsapp/conversations/{public_id}/reopen")
async def reopen_conversation(public_id: str, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    conv.status = "Open"
    conv.tag = "Reopened"
    await db.commit()
    return {"ok": True, "status": conv.status}


@router.get("/api/whatsapp/agents")
async def list_agents(db: Db, user: CurrentUser):
    _ = user
    rows = (
        await db.execute(
            select(Employee).where(Employee.status.in_(["Active", "Probation"])).order_by(Employee.name).limit(200)
        )
    ).scalars().all()
    items = []
    for e in rows:
        open_n = (
            await db.execute(
                select(func.count())
                .select_from(WhatsAppConversation)
                .where(
                    WhatsAppConversation.assigned_employee_id == e.id,
                    WhatsAppConversation.status != "Resolved",
                    _not_spam_filter(),
                )
            )
        ).scalar() or 0
        items.append(
            {
                "id": e.id,
                "name": e.name,
                "department": e.department,
                "languages": e.languages,
                "photo_url": e.photo_url,
                "availability": "Available",
                "open_conversations": open_n,
                "initial": _initials(e.name),
            }
        )
    return {"items": items}


@router.post("/api/whatsapp/messages/{message_id}/transcribe")
async def retry_message_transcription(message_id: str, db: Db, user: CurrentUser):
    """Staff retry for voice STT or text→Malayalam translate (idempotent when completed)."""
    msg = (
        await db.execute(select(WhatsAppMessage).where(WhatsAppMessage.public_id == message_id))
    ).scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    conv = await db.get(WhatsAppConversation, msg.conversation_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    await _get_conversation(db, conv.public_id, user)
    from app.sarvam_transcribe import (
        is_text_translatable_message,
        retry_transcription_by_public_id,
    )

    is_speech = (msg.media_type or "").lower() in {"audio", "ptt", "voice", "video"}
    if not is_speech and not is_text_translatable_message(msg):
        raise HTTPException(400, "Only voice/video or text messages can be transcribed/translated")
    result = await retry_transcription_by_public_id(message_id)
    if result.get("reason") == "not-found":
        raise HTTPException(404, "Message not found")
    return result


@router.get("/api/whatsapp/media/{filename}")
async def get_wa_media(filename: str):
    """Public by unguessable UUID filename — needed for <img>/<audio> without auth headers."""
    path = resolve_path(filename)
    if not path:
        raise HTTPException(404, "Media not found")
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".webm": "audio/webm",
        # WhatsApp PTT is Opus-in-Ogg; codecs hint helps browsers show real duration (not 0:00).
        ".ogg": "audio/ogg; codecs=opus",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")
    # inline so browser displays images/audio instead of forcing download
    return FileResponse(path, media_type=mime, filename=path.name, content_disposition_type="inline")


class VisitingCardExtractIn(BaseModel):
    image_url: str = Field(default="", max_length=500)
    image_base64: str = Field(default="", max_length=5_000_000)
    mime: str = Field(default="image/jpeg", max_length=120)
    resolve_places: bool = True
    source: str = Field(default="Visiting Card", max_length=80)


@router.post("/api/whatsapp/visiting-card/extract")
async def extract_visiting_card_photo(body: VisitingCardExtractIn, user: CurrentUser) -> dict[str, Any]:
    """Gemini vision reads a visiting-card photo; Places fills missing state/district."""
    _ = user
    from app.visiting_card import extract_visiting_card

    if not (body.image_url or "").strip() and not (body.image_base64 or "").strip():
        raise HTTPException(400, "Provide image_url or image_base64")
    try:
        return await extract_visiting_card(
            image_url=body.image_url or "",
            image_base64=body.image_base64 or "",
            mime=body.mime or "image/jpeg",
            resolve_places=bool(body.resolve_places),
            source=(body.source or "Visiting Card").strip() or "Visiting Card",
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc) or "Media not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc) or "Invalid image") from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc) or "AI not configured") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Visiting card extract failed: {type(exc).__name__}") from exc


@router.post("/api/whatsapp/conversations/{public_id}/avatar")
async def refresh_avatar(public_id: str, db: Db, user: CurrentUser):
    """Fetch contact DP / group-channel subject+icon from Evolution, cache on VPS.

    Missing / private DPs are a soft success (empty photo → UI initials), not 502.
    """
    from app.cloud_whatsapp import is_meta_cloud_instance

    conv = await _get_conversation(db, public_id, user)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    if not inst or inst.deleted_at:
        raise HTTPException(404, "Instance missing")
    if is_meta_cloud_instance(inst):
        return {
            "ok": True,
            "customer_photo": conv.customer_photo or "",
            "customer_name": conv.customer_name or "",
            "display_name": conv.customer_name or conv.customer_phone,
            "chat_kind": "dm",
            "provider": "meta_cloud",
        }
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)

    jid = (conv.customer_jid or "").strip()
    kind = chat_kind_from_jid(jid)
    if kind in {"group", "channel"}:
        meta = await enrich_conversation_meta(svc=svc, token=token, conv=conv, force=True)
        if meta.get("changed"):
            await db.commit()
        return {
            "ok": bool(meta.get("ok", True)),
            "customer_photo": conv.customer_photo or "",
            "customer_name": conv.customer_name or "",
            "display_name": conv.customer_name or conv.customer_phone,
            "chat_kind": kind,
            "is_group": kind == "group",
            "is_channel": kind == "channel",
            "reason": meta.get("reason") or "",
        }

    # Prefer stored LID jid when present — PN avatar lookups often hang
    lookup = conv.customer_phone
    if jid.endswith("@lid") or "@lid" in jid:
        lookup = jid
    elif jid.endswith("@s.whatsapp.net"):
        lookup = jid

    try:
        remote = await svc.get_avatar(token, lookup, preview=True)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        # Common Evolution/WA responses when DP is unset or private
        if any(
            x in msg
            for x in (
                "does not have a profile picture",
                "no profile picture",
                "not found",
                "404",
            )
        ):
            return {
                "ok": True,
                "customer_photo": conv.customer_photo or "",
                "customer_name": conv.customer_name or "",
                "chat_kind": kind,
                "reason": "no_picture",
            }
        # Soft-fail other transport errors so list refresh does not spam 502
        return {
            "ok": False,
            "customer_photo": conv.customer_photo or "",
            "customer_name": conv.customer_name or "",
            "chat_kind": kind,
            "reason": str(exc)[:300],
        }

    url = ""
    if isinstance(remote, dict):
        data = remote.get("data") if isinstance(remote.get("data"), dict) else remote
        if isinstance(data, dict):
            url = str(
                data.get("url")
                or data.get("profilePictureUrl")
                or data.get("picture")
                or data.get("avatar")
                or data.get("base64")
                or ""
            )
        elif isinstance(data, str):
            url = data
        # Evolution docs also return top-level avatar base64
        if not url and remote.get("avatar"):
            url = str(remote.get("avatar") or "")

    if not url:
        return {
            "ok": True,
            "customer_photo": conv.customer_photo or "",
            "customer_name": conv.customer_name or "",
            "chat_kind": kind,
            "raw": remote,
        }

    # Cache image on VPS disk so browser does not depend on WhatsApp CDN
    try:
        import httpx

        if url.startswith("http://") or url.startswith("https://"):
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                public_url, _ = save_bytes(
                    r.content, media_type="image", filename=f"{conv.customer_phone or 'dp'}.jpg"
                )
                conv.customer_photo = public_url
        else:
            if not url.startswith("data:"):
                url = f"data:image/jpeg;base64,{url}"
            public_url, _, _ = save_data_url(
                url, media_type="image", filename=f"{conv.customer_phone or 'dp'}.jpg"
            )
            conv.customer_photo = public_url
        await db.commit()
    except Exception:  # noqa: BLE001
        # Fallback: store URL string (may expire) so UI still shows something
        if url.startswith("http"):
            conv.customer_photo = url[:2000]
            await db.commit()
    return {
        "ok": True,
        "customer_photo": conv.customer_photo or "",
        "customer_name": conv.customer_name or "",
        "display_name": conv.customer_name or conv.customer_phone,
        "chat_kind": kind,
    }


@router.post("/api/whatsapp/conversations/{public_id}/sync-history")
async def sync_conversation_history(public_id: str, db: Db, user: CurrentUser):
    """Request on-demand WhatsApp history for one chat (messages arrive via webhooks)."""
    from app.cloud_whatsapp import is_meta_cloud_instance

    conv = await _get_conversation(db, public_id, user)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    if not inst or inst.deleted_at:
        raise HTTPException(404, "Instance missing")
    if is_meta_cloud_instance(inst):
        from app.cloud_whatsapp import sync_care_from_firestore

        wa = "".join(c for c in (conv.customer_phone or "") if c.isdigit())
        try:
            result = await sync_care_from_firestore(db, message_wa_id=wa or None)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "conversation_id": public_id,
                "provider": "meta_cloud",
                "error": str(exc)[:400],
                "note": "Care Firestore sync failed; live updates still arrive via YesOne webhook when configured.",
            }
        return {
            "ok": True,
            "conversation_id": public_id,
            "provider": "meta_cloud",
            "note": "Synced Care messages from YesOne Firestore.",
            **{k: v for k, v in result.items() if k != "ok"},
        }
    if (inst.connection_status or "").lower() != "connected":
        raise HTTPException(409, f"WhatsApp instance is {inst.connection_status}")
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    jid = (conv.customer_jid or "").strip()
    if not jid or "@g.us" in jid.lower() or "@lid" in jid.lower():
        phone = "".join(c for c in (conv.customer_phone or "") if c.isdigit())
        if not phone:
            raise HTTPException(400, "Conversation has no WhatsApp jid/phone")
        jid = f"{phone}@s.whatsapp.net"
    try:
        remote = await svc.request_history_sync(token, jid, count=40)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # No Signal session yet — WA won't share peer history until this device
        # has messaged / received from that chat. Soft response for inbox UX.
        if "no signal session" in msg.lower() or "encrypt" in msg.lower():
            return {
                "ok": False,
                "conversation_id": public_id,
                "jid": jid,
                "reason": "no_signal_session",
                "note": "WhatsApp has no E2E session for this chat on the linked device yet. "
                "History appears after an inbound/outbound message, or via login RECENT sync.",
                "error": msg[:400],
            }
        raise HTTPException(502, f"History request failed: {exc}") from exc
    return {
        "ok": True,
        "conversation_id": public_id,
        "jid": jid,
        "note": "History requested. Messages appear when WhatsApp delivers them via webhook.",
        "remote": remote if isinstance(remote, dict) else {},
    }


@router.post("/api/whatsapp/inbox/test-ping")
async def test_ping(db: Db, user: CurrentUser):
    """Admin-only: send a self-test message via the first connected YesWeigh instance."""
    _require_admin(user)
    # Prefer Evolution personal lines for test-ping (never Care Cloud).
    inst = (
        await db.execute(
            select(WhatsAppInstance)
            .where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
                WhatsAppInstance.connection_status == "Connected",
                WhatsAppInstance.provider != "meta_cloud",
            )
            .order_by(WhatsAppInstance.id.desc())
        )
    ).scalars().first()
    if not inst:
        raise HTTPException(409, "No connected WhatsApp instance")
    phone = _phone_digits(inst.connected_phone_number)
    if not phone:
        raise HTTPException(409, "Connected instance has no phone number")
    text = f"YesWeigh CRM inbox test · {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    return await create_conversation(
        NewChatIn(
            instance_id=inst.public_id,
            customer_phone=phone,
            customer_name=inst.profile_name or "Self test",
            message=text,
        ),
        db,
        user,
    )
