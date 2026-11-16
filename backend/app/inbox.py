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

from app.auth import get_current_user
from app.crypto_util import decrypt_secret
from app.db import get_db
from app.evolution import ORG, _owned_instance, _require_admin, _svc
from app.media_store import PUBLIC_PREFIX, resolve_path, save_bytes, save_data_url, to_evolution_base64
from app.models import (
    Employee,
    User,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppInstanceEmployee,
    WhatsAppMessage,
)

router = APIRouter(tags=["whatsapp-inbox"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


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
    if not emp:
        return []
    rows = (
        await db.execute(
            select(WhatsAppInstanceEmployee.whatsapp_instance_id).where(
                WhatsAppInstanceEmployee.employee_id == emp.id,
                WhatsAppInstanceEmployee.is_active.is_(True),
                WhatsAppInstanceEmployee.organization_id == ORG,
            )
        )
    ).scalars().all()
    return list(rows)


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


def _conv_out(
    conv: WhatsAppConversation,
    *,
    account: str = "",
    agent: str = "",
) -> dict[str, Any]:
    name = conv.customer_name or conv.customer_phone
    return {
        "id": conv.public_id,
        "customer_name": conv.customer_name,
        "customer_phone": conv.customer_phone,
        "customer_photo": conv.customer_photo or "",
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
        "customer_type": conv.customer_type,
        "notes_summary": conv.notes_summary,
        "created_at": conv.created_at,
    }


def _msg_out(msg: WhatsAppMessage) -> dict[str, Any]:
    media = msg.media_url or ""
    # Never return raw data:/base64 blobs to the client — only public paths or http(s)
    if media.startswith("data:"):
        media = ""
    elif len(media) > 2000 and not media.startswith(("http://", "https://", PUBLIC_PREFIX)):
        media = ""
    return {
        "id": msg.public_id,
        "direction": msg.direction,
        "body": msg.body,
        "media_url": media,
        "media_type": msg.media_type,
        "media_name": msg.media_name,
        "status": msg.status,
        "sender_name": msg.sender_name,
        "created_at": msg.created_at,
        "external_message_id": msg.external_message_id,
        "is_voice": msg.media_type == "audio",
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
    return {
        "items": [
            {
                "id": r.public_id,
                "pk": r.id,
                "display_name": r.display_name,
                "connection_status": r.connection_status,
                "connected_phone_number": r.connected_phone_number,
                "purpose": r.purpose,
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

    stmt = select(WhatsAppConversation).where(WhatsAppConversation.organization_id == ORG)
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
            )
        )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0

    if sort == "oldest":
        stmt = stmt.order_by(WhatsAppConversation.last_message_at.asc().nullslast())
    elif sort == "unread":
        stmt = stmt.order_by(WhatsAppConversation.unread_count.desc(), WhatsAppConversation.last_message_at.desc().nullslast())
    else:
        stmt = stmt.order_by(WhatsAppConversation.last_message_at.desc().nullslast())

    rows = (await db.execute(stmt.offset(offset).limit(limit))).scalars().all()

    inst_ids = {r.whatsapp_instance_id for r in rows}
    emp_ids = {r.assigned_employee_id for r in rows if r.assigned_employee_id}
    inst_map = {}
    if inst_ids:
        for i in (
            await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.id.in_(inst_ids)))
        ).scalars().all():
            inst_map[i.id] = i.display_name
    emp_map = {}
    if emp_ids:
        for e in (await db.execute(select(Employee).where(Employee.id.in_(emp_ids)))).scalars().all():
            emp_map[e.id] = e.name

    items = [
        _conv_out(r, account=inst_map.get(r.whatsapp_instance_id, ""), agent=emp_map.get(r.assigned_employee_id or 0, ""))
        for r in rows
    ]
    metrics = await _metrics(db, allowed)
    return {"items": items, "total": total, "metrics": metrics}


def _empty_metrics() -> dict[str, Any]:
    return {
        "open": 0,
        "unread": 0,
        "unassigned": 0,
        "agents_online": 0,
        "agents_total": 0,
    }


async def _metrics(db: AsyncSession, allowed: list[int] | None) -> dict[str, Any]:
    base = select(WhatsAppConversation).where(WhatsAppConversation.organization_id == ORG)
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
    return {
        "open": open_n,
        "unread": int(unread_n),
        "unassigned": unassigned_n,
        "agents_online": agents_total,
        "agents_total": agents_total,
    }


@router.get("/api/whatsapp/conversations/{public_id}")
async def get_conversation(public_id: str, db: Db, user: CurrentUser):
    conv = await _get_conversation(db, public_id, user)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    agent = ""
    if conv.assigned_employee_id:
        emp = await db.get(Employee, conv.assigned_employee_id)
        agent = emp.name if emp else ""
    return _conv_out(conv, account=inst.display_name if inst else "", agent=agent)


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
    if existing:
        conv = existing
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
            assigned_user_id=user.id,
            assigned_employee_id=emp.id if emp else None,
        )
        db.add(conv)
        await db.flush()

    if body.message.strip():
        await _send_and_store(db, user, conv, inst, body.message.strip())

    await db.commit()
    await db.refresh(conv)
    return _conv_out(conv, account=inst.display_name, agent=emp.name if emp else user.name)


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
    if client_message_id:
        dup = (
            await db.execute(
                select(WhatsAppMessage).where(WhatsAppMessage.client_message_id == client_message_id)
            )
        ).scalar_one_or_none()
        if dup:
            return dup

    emp = await _employee_for_user(db, user)

    # Persist media on VPS disk; Postgres keeps only the public path + metadata
    stored_media_url = media_url or ""
    evo_media_payload = media_url or ""
    if media_url and media_type:
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

    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    recipient = _send_recipient(conv)
    if not recipient:
        raise HTTPException(400, "Conversation has no valid WhatsApp recipient")
    try:
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
    if conv.status in {"New", "Pending Customer Reply", "Resolved"}:
        conv.status = "Open"
    if not conv.assigned_user_id:
        conv.assigned_user_id = user.id
        if emp:
            conv.assigned_employee_id = emp.id
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
    conv = await _get_conversation(db, public_id, user)
    conv.unread_count = 0
    await db.commit()
    return {"ok": True, "unread_count": 0}


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
    """Fetch contact DP from Evolution (LID-aware), cache file on VPS, path in Postgres."""
    conv = await _get_conversation(db, public_id, user)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    if not inst or inst.deleted_at:
        raise HTTPException(404, "Instance missing")
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)

    # Prefer stored LID jid when present — PN avatar lookups often hang
    lookup = conv.customer_phone
    jid = (conv.customer_jid or "").strip()
    if jid.endswith("@lid") or "@lid" in jid:
        lookup = jid

    try:
        remote = await svc.get_avatar(token, lookup, preview=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Avatar fetch failed: {exc}") from exc

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
        return {"ok": True, "customer_photo": conv.customer_photo or "", "raw": remote}

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
    return {"ok": True, "customer_photo": conv.customer_photo or ""}


@router.post("/api/whatsapp/inbox/test-ping")
async def test_ping(db: Db, user: CurrentUser):
    """Admin-only: send a self-test message via the first connected YesWeigh instance."""
    _require_admin(user)
    inst = (
        await db.execute(
            select(WhatsAppInstance)
            .where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
                WhatsAppInstance.connection_status == "Connected",
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
