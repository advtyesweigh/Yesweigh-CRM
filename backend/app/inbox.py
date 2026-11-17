"""Multi-agent WhatsApp Inbox APIs — YesWeigh org instances only."""
from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
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
from app.dealers import _normalized_phone_sql
from app.models import (
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
from app.phone_util import normalize_in_mobile
from app.whatsapp_chat_meta import chat_kind_from_jid, enrich_conversation_meta
from app.whatsapp_chat_types import newsletter_list_filter

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
    """Hide @lid shells when a PN row on the same instance already stores this LID."""
    twin = aliased(WhatsAppConversation)
    self_lid = func.coalesce(
        func.nullif(func.trim(WhatsAppConversation.customer_lid), ""),
        func.nullif(
            func.regexp_replace(
                func.split_part(WhatsAppConversation.customer_jid, "@", 1),
                r":.*$",
                "",
            ),
            "",
        ),
    )
    twin_is_pn = or_(
        twin.customer_jid.ilike("%@s.whatsapp.net"),
        and_(
            ~twin.customer_jid.ilike("%@lid"),
            func.length(func.regexp_replace(twin.customer_phone, r"\D", "", "g")).between(10, 15),
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
            self_lid != "",
            twin.customer_lid == self_lid,
        )
        .correlate(WhatsAppConversation)
        .exists()
    )
    return ~and_(WhatsAppConversation.customer_jid.ilike("%@lid"), has_pn_twin)


def _is_admin(user: User) -> bool:
    return user.role.lower() in {"admin", "super_admin", "super admin", "management"}


def _phone_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _send_recipient(conv: WhatsAppConversation) -> str:
    """
    Evolution GO needs a real PN or an explicit @lid jid.
    Bare LID digits get wrongly treated as @s.whatsapp.net and fail.
    """
    jid = (conv.customer_jid or "").strip()
    phone = _phone_digits(conv.customer_phone)
    if jid.endswith("@lid"):
        lid = _phone_digits(jid.split("@")[0])
        # Prefer real phone when we have it and it differs from the LID
        if phone and phone != lid and len(phone) <= 15:
            return phone
        return f"{lid}@lid" if lid else jid
    if phone:
        return phone
    if "@" in jid:
        return jid
    return conv.customer_phone or ""


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


def _dealer_brief(dealer: Dealer | None) -> dict[str, Any] | None:
    if not dealer:
        return None
    return {
        "id": dealer.id,
        "name": dealer.name or "",
        "company": dealer.company or "",
        "phone": dealer.phone or "",
        "code": dealer.zoho_contact_id or "",
        "city": dealer.city or "",
        "state": dealer.state or "",
    }


def _conv_out(
    conv: WhatsAppConversation,
    *,
    account: str = "",
    agent: str = "",
    dealer: Dealer | None = None,
    provider: str = "",
    last_inbound_at: datetime | None = None,
) -> dict[str, Any]:
    name = conv.customer_name or conv.customer_phone
    linked = _dealer_brief(dealer)
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
        "notes_summary": conv.notes_summary,
        "created_at": conv.created_at,
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
) -> dict[str, Any]:
    if inst is None:
        inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    provider = ((getattr(inst, "provider", None) or "evolution") if inst else "").strip().lower()
    last_in = await _last_inbound_at(db, conv.id) if provider == "meta_cloud" else None
    return _conv_out(
        conv,
        account=inst.display_name if inst else "",
        agent=agent,
        dealer=dealer,
        provider=provider,
        last_inbound_at=last_in,
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


class ContactUpdateIn(BaseModel):
    customer_name: str | None = None
    region: str | None = None
    language: str | None = None
    customer_type: str | None = None


class LinkDealerIn(BaseModel):
    dealer_id: int


@router.get("/api/whatsapp/inbox/accounts")
async def inbox_accounts(db: Db, user: CurrentUser):
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
    unread_by_inst: dict[int, int] = {}
    if rows:
        inst_pks = [r.id for r in rows]
        unread_rows = (
            await db.execute(
                select(
                    WhatsAppConversation.whatsapp_instance_id,
                    func.coalesce(func.sum(WhatsAppConversation.unread_count), 0),
                )
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.whatsapp_instance_id.in_(inst_pks),
                    newsletter_list_filter(),
                )
                .group_by(WhatsAppConversation.whatsapp_instance_id)
            )
        ).all()
        unread_by_inst = {int(iid): int(n or 0) for iid, n in unread_rows}
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
                "unread_count": unread_by_inst.get(r.id, 0),
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
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    allowed = await _authorized_instance_ids(db, user)
    if allowed is not None and not allowed:
        return {"items": [], "total": 0, "metrics": _empty_metrics()}

    stmt = select(WhatsAppConversation).where(
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        _lid_orphan_hidden_filter(),
    )
    if allowed is not None:
        stmt = stmt.where(WhatsAppConversation.whatsapp_instance_id.in_(allowed))

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

    activity = _activity_at()
    if sort == "oldest":
        stmt = stmt.order_by(activity.asc().nullslast())
    elif sort == "unread":
        stmt = stmt.order_by(WhatsAppConversation.unread_count.desc(), activity.desc().nullslast())
    else:
        stmt = stmt.order_by(activity.desc().nullslast())

    # Skip exact COUNT(*) — inbox FE does not paginate by total; saves a full scan per chip toggle.
    rows = (await db.execute(stmt.offset(offset).limit(limit))).scalars().all()
    total = offset + len(rows)

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

    care_conv_ids = [r.id for r in rows if inst_provider.get(r.whatsapp_instance_id) == "meta_cloud"]
    inbound_map = await _last_inbound_at_map(db, care_conv_ids)

    items = [
        _conv_out(
            r,
            account=inst_name.get(r.whatsapp_instance_id, ""),
            agent=emp_map.get(r.assigned_employee_id or 0, ""),
            dealer=dealer_map.get(r.dealer_id) if getattr(r, "dealer_id", None) else None,
            provider=inst_provider.get(r.whatsapp_instance_id, ""),
            last_inbound_at=inbound_map.get(r.id),
        )
        for r in rows
    ]
    metrics = await _metrics(db, allowed)
    return {"items": items, "total": total, "metrics": metrics}


def _empty_metrics() -> dict[str, Any]:
    return {
        "open": 0,
        "unread": 0,
        "unread_conversations": 0,
        "unassigned": 0,
        "agents_online": 0,
        "agents_total": 0,
    }


async def _metrics(db: AsyncSession, allowed: list[int] | None) -> dict[str, Any]:
    base = select(WhatsAppConversation).where(
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        _lid_orphan_hidden_filter(),
    )
    if allowed is not None:
        if not allowed:
            return _empty_metrics()
        base = base.where(WhatsAppConversation.whatsapp_instance_id.in_(allowed))
    open_q = base.where(WhatsAppConversation.status != "Resolved")
    open_n = (await db.execute(select(func.count()).select_from(open_q.subquery()))).scalar() or 0
    unread_n = (
        await db.execute(
            select(func.coalesce(func.sum(WhatsAppConversation.unread_count), 0)).where(
                WhatsAppConversation.organization_id == ORG,
                newsletter_list_filter(),
                *([] if allowed is None else [WhatsAppConversation.whatsapp_instance_id.in_(allowed)]),
            )
        )
    ).scalar() or 0
    unassigned_n = (
        await db.execute(
            select(func.count()).select_from(
                open_q.where(
                    WhatsAppConversation.assigned_employee_id.is_(None),
                    WhatsAppConversation.assigned_user_id.is_(None),
                ).subquery()
            )
        )
    ).scalar() or 0
    agents_total = (
        await db.execute(
            select(func.count()).select_from(Employee).where(Employee.status.in_(["Active", "Probation"]))
        )
    ).scalar() or 0
    unread_convs = (
        await db.execute(
            select(func.count()).select_from(
                base.where(WhatsAppConversation.unread_count > 0).subquery()
            )
        )
    ).scalar() or 0
    return {
        "open": open_n,
        "unread": int(unread_n),  # sum of unread inbound messages from phone
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


@router.get("/api/whatsapp/conversations/{public_id}/dealers/search")
async def search_dealers_for_conversation(
    public_id: str,
    db: Db,
    user: CurrentUser,
    q: str = Query(""),
    limit: int = Query(12, ge=1, le=40),
):
    """Search dealers to link — same last-10 phone overlap pattern used elsewhere."""
    await _get_conversation(db, public_id, user)
    stmt = select(Dealer).order_by(Dealer.name.asc()).limit(limit)
    term = q.strip()
    if term:
        like = f"%{term}%"
        phone_key = normalize_in_mobile(term)
        clauses = [
            Dealer.name.ilike(like),
            Dealer.company.ilike(like),
            Dealer.phone.ilike(like),
            Dealer.zoho_contact_id.ilike(like),
            Dealer.city.ilike(like),
        ]
        if phone_key:
            clauses.append(_normalized_phone_sql(Dealer.phone) == phone_key)
        stmt = stmt.where(or_(*clauses))
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "id": d.id,
                "name": d.name or "",
                "company": d.company or "",
                "phone": d.phone or "",
                "code": d.zoho_contact_id or "",
                "city": d.city or "",
                "state": d.state or "",
            }
            for d in rows
        ]
    }


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
            await db.commit()
            await db.refresh(conv)
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
    if media_url and media_type:
        if care:
            raise HTTPException(400, "Care Cloud WhatsApp replies support text only for now")
        try:
            if media_url.startswith("data:") or (
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

    recipient = _send_recipient(conv)
    if not recipient:
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
    except HTTPException:
        msg.status = "failed"
        msg.body = text
        conv.last_message_preview = f"Failed: {text[:200]}"
        await db.flush()
        raise
    except Exception as exc:  # noqa: BLE001
        msg.status = "failed"
        msg.body = text
        conv.last_message_preview = f"Failed: {text[:200]}"
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
    clear_conversation_unread(conv)
    if conv.status in {"Pending Customer Reply", "Resolved"}:
        conv.status = "Open"
    if not conv.assigned_employee_id:
        owner = await assign_conversation_to_instance_owner(db, conv, inst.id)
        if not owner and not conv.assigned_user_id:
            conv.assigned_user_id = user.id
            if emp:
                conv.assigned_employee_id = emp.id

    # Auto-transcribe outbound voice after media is stored locally.
    if (media_type or "").lower() in {"audio", "ptt", "voice"} and (
        stored_media_url or ""
    ).startswith(PUBLIC_PREFIX):
        try:
            from app.sarvam_transcribe import schedule_transcription

            schedule_transcription(msg.id)
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


@router.post("/api/whatsapp/conversations/{public_id}/read")
async def mark_read(public_id: str, db: Db, user: CurrentUser):
    """Mark conversation read when an agent opens it in CRM (or after reply)."""
    conv = await _get_conversation(db, public_id, user)
    clear_conversation_unread(conv)

    # Best-effort WhatsApp read receipts via Evolution (does not block CRM read).
    try:
        inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
        recipient = _send_recipient(conv)
        if inst and recipient:
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
            if ids:
                _, svc = await _svc(db)
                token = decrypt_secret(inst.encrypted_instance_token)
                await svc.mark_read(token, recipient, ids)
    except Exception:  # noqa: BLE001
        pass

    await db.commit()
    return {
        "ok": True,
        "unread_count": 0,
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
    """Staff retry for Sarvam STT on a voice note (idempotent when already completed)."""
    msg = (
        await db.execute(select(WhatsAppMessage).where(WhatsAppMessage.public_id == message_id))
    ).scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    conv = await db.get(WhatsAppConversation, msg.conversation_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    await _get_conversation(db, conv.public_id, user)
    if (msg.media_type or "").lower() not in {"audio", "ptt", "voice"}:
        raise HTTPException(400, "Only voice notes can be transcribed")
    from app.sarvam_transcribe import retry_transcription_by_public_id

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
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")
    # inline so browser displays images/audio instead of forcing download
    return FileResponse(path, media_type=mime, filename=path.name, content_disposition_type="inline")


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
            result = await sync_care_from_firestore(db, message_wa_id=wa or None, conversation_limit=5)
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
