"""Safeguards for AI Agent WhatsApp sends: self/staff block + duplicate skip."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CampaignMedia,
    CampaignRun,
    Employee,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppInstanceEmployee,
    WhatsAppMessage,
)
from app.phone_util import normalize_in_mobile

ORG = "yesweigh"

# Skip re-send of identical text from same instance → same phone within this window.
_DEFAULT_DEDUP_HOURS = 24
_SUCCESS_STATUSES = frozenset({"sent", "delivered", "read", "sending", "queued"})
# Soft token-overlap threshold for near-duplicate English cores (on/from Tuesday, etc.).
_ENGLISH_CORE_OVERLAP = 0.80
# Campaign / ops notices that must not be re-blasted with light rewording.
_NOTICE_KEYWORD_GROUPS = (
    frozenset({"container", "strike"}),
    frozenset({"dispatch", "resume"}),
)
_SIGNATURE_RE = re.compile(r"\s*[-–—]\s*yesweigh\s*$", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def dedup_window_hours() -> int:
    raw = (os.environ.get("WHATSAPP_SEND_DEDUP_HOURS") or "").strip()
    if not raw:
        return _DEFAULT_DEDUP_HOURS
    try:
        return max(1, min(168, int(raw)))
    except ValueError:
        return _DEFAULT_DEDUP_HOURS


def campaign_media_dedup_window_hours() -> int | None:
    """Hours window for campaign-media dedupe; None = all-time (cross-campaign / cross-instance)."""
    raw = (os.environ.get("WHATSAPP_CAMPAIGN_MEDIA_DEDUP_HOURS") or "").strip().lower()
    if raw in {"", "0", "forever", "all", "none"}:
        return None
    try:
        return max(1, min(8760, int(raw)))
    except ValueError:
        return None


def phone_key(value: str | None) -> str:
    """Stable 10-digit (IN) key for comparing phones."""
    n = normalize_in_mobile(value)
    if n:
        return n
    digits = re.sub(r"\D+", "", value or "")
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def _add_phone_keys(keys: set[str], *values: str | None) -> None:
    for v in values:
        k = phone_key(v)
        if k and len(k) >= 8:
            keys.add(k)


async def load_org_staff_phone_keys(db: AsyncSession) -> set[str]:
    """All known YesWeigh instance + employee phone keys (never treat as customers)."""
    keys: set[str] = set()
    inst_rows = (
        await db.execute(
            select(
                WhatsAppInstance.connected_phone_number,
                WhatsAppInstance.expected_phone_number,
            ).where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
            )
        )
    ).all()
    for connected, expected in inst_rows:
        _add_phone_keys(keys, connected, expected)

    emp_rows = (
        await db.execute(
            select(Employee.phone, Employee.whatsapp).where(
                or_(
                    Employee.status == "Active",
                    Employee.status == "",
                    Employee.status.is_(None),
                )
            )
        )
    ).all()
    for phone, wa in emp_rows:
        _add_phone_keys(keys, phone, wa)
    return keys


def instance_self_phone_keys(inst: WhatsAppInstance) -> set[str]:
    keys: set[str] = set()
    _add_phone_keys(keys, inst.connected_phone_number, inst.expected_phone_number)
    return keys


async def load_instance_linked_employee_phone_keys(
    db: AsyncSession, instance_id: int
) -> set[str]:
    """Phones of employees assigned to this WhatsApp instance."""
    keys: set[str] = set()
    rows = (
        await db.execute(
            select(Employee.phone, Employee.whatsapp)
            .join(
                WhatsAppInstanceEmployee,
                WhatsAppInstanceEmployee.employee_id == Employee.id,
            )
            .where(
                WhatsAppInstanceEmployee.whatsapp_instance_id == instance_id,
                WhatsAppInstanceEmployee.is_active.is_(True),
                WhatsAppInstanceEmployee.revoked_at.is_(None),
            )
        )
    ).all()
    for phone, wa in rows:
        _add_phone_keys(keys, phone, wa)
    return keys


async def blocked_recipient_keys_for_send(
    db: AsyncSession, inst: WhatsAppInstance
) -> tuple[set[str], set[str]]:
    """Return (self_keys, staff_keys) for TO validation.

    self_keys = FROM instance own numbers + linked employee numbers for that instance.
    staff_keys = org-wide instance + employee numbers (includes self_keys).
    """
    self_keys = instance_self_phone_keys(inst)
    linked = await load_instance_linked_employee_phone_keys(db, inst.id)
    self_keys |= linked
    staff_keys = await load_org_staff_phone_keys(db)
    staff_keys |= self_keys
    return self_keys, staff_keys


def classify_blocked_recipient(
    to_phone: str,
    *,
    self_keys: set[str],
    staff_keys: set[str],
) -> str | None:
    """Return 'self', 'staff', or None if the recipient is allowed."""
    key = phone_key(to_phone)
    if not key:
        return None
    if key in self_keys:
        return "self"
    if key in staff_keys:
        return "staff"
    return None


def blocked_recipient_error(kind: str, to_phone: str, inst: WhatsAppInstance) -> str:
    from_label = (
        (inst.employee_name or inst.display_name or inst.evolution_instance_name or "agent")
        .strip()
    )
    phone_disp = phone_key(to_phone) or to_phone
    if kind == "self":
        return (
            f"Refusing self-message: TO {phone_disp} is the FROM instance's own number "
            f"(or a linked employee for {from_label}). Never message the sending WhatsApp "
            f"line itself. 'from {from_label}' means send VIA that instance, not TO them."
        )
    return (
        f"Refusing staff recipient: TO {phone_disp} belongs to a YesWeigh employee/"
        f"WhatsApp instance. Do not add staff as customers. "
        f"'from {from_label}' means send VIA that agent's Evolution instance, not TO them."
    )


async def _matching_conversation_ids(
    db: AsyncSession,
    *,
    instance_id: int,
    to_phone: str,
) -> list[int]:
    key = phone_key(to_phone)
    if not key:
        return []
    conv_rows = (
        await db.execute(
            select(WhatsAppConversation.id, WhatsAppConversation.customer_phone).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == instance_id,
                or_(
                    WhatsAppConversation.customer_phone == key,
                    WhatsAppConversation.customer_phone == to_phone,
                    WhatsAppConversation.customer_phone.ilike(f"%{key}"),
                ),
            )
        )
    ).all()
    conv_ids: list[int] = []
    for cid, cphone in conv_rows:
        if phone_key(cphone) == key or (cphone or "") == to_phone:
            conv_ids.append(cid)
    return conv_ids


def _dedup_hit(row: WhatsAppMessage, window: int) -> dict[str, Any] | None:
    if not row:
        return None
    status = (row.status or "").strip().lower()
    # Allow retry only when the prior attempt clearly failed.
    if status == "failed":
        return None
    if status and status not in _SUCCESS_STATUSES:
        # Unknown status — still treat as already attempted to avoid doubles.
        pass
    return {
        "message_id": row.public_id,
        "sent_at": row.created_at.isoformat() if row.created_at else None,
        "status": row.status or "",
        "dedup_hours": window if window else None,
    }


def normalize_outbound_text(text: str | None) -> str:
    """Collapse whitespace and strip punctuation for stable outbound comparison."""
    t = (text or "").lower().replace("\u00a0", " ")
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def english_core_text(text: str | None) -> str:
    """English / Latin lines only (ignore Malayalam twin line + trailing - YesWeigh)."""
    lines: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = _SIGNATURE_RE.sub("", line).strip()
        if not line:
            continue
        non_space = sum(1 for c in line if not c.isspace())
        if non_space <= 0:
            continue
        ascii_letters = sum(1 for c in line if ("A" <= c <= "Z") or ("a" <= c <= "z"))
        if ascii_letters / non_space < 0.45:
            continue
        lines.append(line)
    return normalize_outbound_text(" ".join(lines))


def _token_overlap(a: str, b: str) -> float:
    ta = {t for t in a.split() if len(t) > 1}
    tb = {t for t in b.split() if len(t) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta | tb), 1)


def texts_are_duplicate(a: str | None, b: str | None) -> bool:
    """True when outbound bodies match after normalize / English-core / soft overlap."""
    na = normalize_outbound_text(a)
    nb = normalize_outbound_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ea = english_core_text(a)
    eb = english_core_text(b)
    if ea and eb:
        if ea == eb:
            return True
        if min(len(ea.split()), len(eb.split())) >= 4 and _token_overlap(ea, eb) >= _ENGLISH_CORE_OVERLAP:
            return True
    return False


def looks_like_container_strike_notice(text: str | None) -> bool:
    """Ops blast about container strike / resume dispatch (EN or mixed)."""
    n = normalize_outbound_text(text)
    if not n:
        return False
    e = english_core_text(text) or n
    hay = f"{n} {e}"
    return all(any(k in hay for k in group) for group in _NOTICE_KEYWORD_GROUPS)


async def already_sent_same_text(
    db: AsyncSession,
    *,
    instance_id: int,
    to_phone: str,
    message: str,
    hours: int | None = None,
) -> dict[str, Any] | None:
    """If same/near-same outbound text was already sent to this phone from this instance recently.

    Matches exact body, normalized body, English-core (Malayalam line ignored), and
    soft English token overlap. Container-strike notices also match other strike notices
    in the same window even when lightly reworded.
    """
    body = (message or "").strip()
    if not body:
        return None
    conv_ids = await _matching_conversation_ids(db, instance_id=instance_id, to_phone=to_phone)
    if not conv_ids:
        return None
    window = hours if hours is not None else dedup_window_hours()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window)
    strike_notice = looks_like_container_strike_notice(body)

    # Prefer exact SQL hit when possible, then scan recent outs for fuzzy matches.
    exact = (
        await db.execute(
            select(WhatsAppMessage)
            .where(
                WhatsAppMessage.organization_id == ORG,
                WhatsAppMessage.whatsapp_instance_id == instance_id,
                WhatsAppMessage.conversation_id.in_(conv_ids),
                WhatsAppMessage.direction == "out",
                WhatsAppMessage.body == body,
                WhatsAppMessage.created_at >= cutoff,
            )
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    hit = _dedup_hit(exact, window)
    if hit:
        return hit

    rows = list(
        (
            await db.execute(
                select(WhatsAppMessage)
                .where(
                    WhatsAppMessage.organization_id == ORG,
                    WhatsAppMessage.whatsapp_instance_id == instance_id,
                    WhatsAppMessage.conversation_id.in_(conv_ids),
                    WhatsAppMessage.direction == "out",
                    WhatsAppMessage.created_at >= cutoff,
                )
                .order_by(WhatsAppMessage.created_at.desc())
                .limit(80)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        prior_body = row.body or ""
        if texts_are_duplicate(body, prior_body):
            return _dedup_hit(row, window)
        if strike_notice and looks_like_container_strike_notice(prior_body):
            # Same ops notice family — skip re-blast even if phrasing drifted.
            ea = english_core_text(body)
            eb = english_core_text(prior_body)
            if ea and eb and _token_overlap(ea, eb) >= 0.55:
                return _dedup_hit(row, window)
    return None


async def find_recent_outbound_echo_twin(
    db: AsyncSession,
    *,
    instance_id: int,
    conversation_id: int,
    body: str,
    within_seconds: int = 900,
) -> WhatsAppMessage | None:
    """CRM outbound row that a fromMe webhook is echoing (body twin, short window)."""
    text = (body or "").strip()
    if not text or not conversation_id:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(30, within_seconds))
    rows = list(
        (
            await db.execute(
                select(WhatsAppMessage)
                .where(
                    WhatsAppMessage.organization_id == ORG,
                    WhatsAppMessage.whatsapp_instance_id == instance_id,
                    WhatsAppMessage.conversation_id == conversation_id,
                    WhatsAppMessage.direction == "out",
                    WhatsAppMessage.created_at >= cutoff,
                )
                .order_by(WhatsAppMessage.created_at.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    # Prefer CRM-authored rows (employee/user sender) over prior WhatsApp echoes.
    ranked = sorted(
        rows,
        key=lambda r: (
            0 if (r.sender_employee_id or r.sender_user_id or (r.client_message_id or "").strip()) else 1,
            0 if texts_are_duplicate(text, r.body or "") else 1,
        ),
    )
    for row in ranked:
        if texts_are_duplicate(text, row.body or ""):
            return row
    return None


async def _matching_conversation_ids_any_instance(
    db: AsyncSession, *, to_phone: str
) -> list[int]:
    """Conversation ids for this phone on any org instance (cross-agent media dedupe)."""
    key = phone_key(to_phone)
    if not key:
        return []
    conv_rows = (
        await db.execute(
            select(WhatsAppConversation.id, WhatsAppConversation.customer_phone).where(
                WhatsAppConversation.organization_id == ORG,
                or_(
                    WhatsAppConversation.customer_phone == key,
                    WhatsAppConversation.customer_phone == to_phone,
                    WhatsAppConversation.customer_phone.ilike(f"%{key}"),
                ),
            )
        )
    ).all()
    conv_ids: list[int] = []
    for cid, cphone in conv_rows:
        if phone_key(cphone) == key or (cphone or "") == to_phone:
            conv_ids.append(cid)
    return conv_ids


async def already_sent_same_media(
    db: AsyncSession,
    *,
    instance_id: int,
    to_phone: str,
    media_name: str,
    caption: str = "",
    campaign_media_id: int | None = None,
    hours: int | None = None,
) -> dict[str, Any] | None:
    """If same outbound media was already sent to this phone recently.

    Media-only dedupe: matches the same campaign_media id / media_name marker.
    Never treats prior text (e.g. container-strike notices) as duplicates of video/image
    sends — text anti-dupe is separate (already_sent_same_text).
    Checks this instance first, then any org instance (so Farook/Visakh/AI Agent
    successes skip re-send of the same campaign media to the same phone).
    Failed priors do not block retry.
    """
    name = (media_name or "").strip()
    if not name and campaign_media_id is None:
        return None
    if campaign_media_id is not None and hours is None:
        window = campaign_media_dedup_window_hours()
        if window is None:
            window = 0  # reported in hit payload only
    else:
        window = hours if hours is not None else dedup_window_hours()
    cutoff = (
        None
        if campaign_media_id is not None and hours is None and campaign_media_dedup_window_hours() is None
        else datetime.now(timezone.utc) - timedelta(hours=window or dedup_window_hours())
    )
    caption_norm = (caption or "").strip()
    marker = f"campaign_media:{campaign_media_id}" if campaign_media_id is not None else ""

    # Prefer same-instance conversations, then expand to any instance for campaign media.
    same_ids = await _matching_conversation_ids(db, instance_id=instance_id, to_phone=to_phone)
    any_ids = await _matching_conversation_ids_any_instance(db, to_phone=to_phone)
    # Keep order: same-instance first, then others
    seen_ids: set[int] = set()
    conv_ids: list[int] = []
    for cid in same_ids + any_ids:
        if cid not in seen_ids:
            seen_ids.add(cid)
            conv_ids.append(cid)
    if not conv_ids:
        return None

    q = (
        select(WhatsAppMessage)
        .where(
            WhatsAppMessage.organization_id == ORG,
            WhatsAppMessage.conversation_id.in_(conv_ids),
            WhatsAppMessage.direction == "out",
            WhatsAppMessage.media_type != "",
            WhatsAppMessage.media_type.is_not(None),
        )
        .order_by(WhatsAppMessage.created_at.desc())
        .limit(80)
    )
    if cutoff is not None:
        q = q.where(WhatsAppMessage.created_at >= cutoff)
    rows = list((await db.execute(q)).scalars().all())
    equiv_ids: set[int] | None = None
    if campaign_media_id is not None:
        equiv_ids = await campaign_media_equivalent_ids(db, campaign_media_id)

    for row in rows:
        # Text-only / empty media_type rows are excluded by SQL; still refuse body-only hits.
        row_media_type = (row.media_type or "").strip().lower()
        if not row_media_type or row_media_type in {"text", "chat"}:
            continue
        row_name = (row.media_name or "").strip()
        row_body = (row.body or "").strip()
        # Require same campaign media marker or exact media_name — never keyword/text overlap.
        name_hit = bool(name) and row_name == name
        marker_hit = bool(marker) and (
            row_name.startswith(marker + ":")
            or row_name == marker
            or marker + ":" in row_name
            or (marker in (row.media_url or "") and "campaign_media:" in (row.media_url or ""))
        )
        if equiv_ids and not marker_hit:
            for mid in equiv_ids:
                em = f"campaign_media:{mid}"
                if row_name.startswith(em + ":") or row_name == em:
                    marker_hit = True
                    break
        if not (name_hit or marker_hit):
            continue
        if caption_norm and row_body != caption_norm:
            # Caption-less prior still blocks identical media with empty caption only
            if not (not caption_norm and not row_body):
                continue
        elif caption_norm != row_body and caption_norm:
            continue
        hit = _dedup_hit(row, window)
        if hit:
            return hit

    if campaign_media_id is not None:
        pk = phone_key(to_phone)
        if pk:
            sent_keys = await load_phones_with_prior_campaign_media(
                db,
                campaign_media_id=campaign_media_id,
                include_pending_on_other_runs=False,
            )
            if pk in sent_keys:
                return {
                    "message_id": None,
                    "sent_at": None,
                    "status": "campaign_run",
                    "dedup_hours": window if window else None,
                }
    return None


async def campaign_media_equivalent_ids(db: AsyncSession, media_id: int) -> set[int]:
    """Campaign media ids that share the same stored file (duplicate uploads)."""
    ids: set[int] = {int(media_id)}
    row = await db.get(CampaignMedia, media_id)
    if not row:
        return ids
    stored = (row.stored_name or "").strip()
    if stored:
        rows = (
            await db.execute(
                select(CampaignMedia.id).where(CampaignMedia.stored_name == stored)
            )
        ).scalars().all()
        ids.update(int(x) for x in rows if x is not None)
    return ids


def _media_markers_for_ids(media_ids: set[int]) -> list[str]:
    return [f"campaign_media:{int(mid)}" for mid in sorted(media_ids)]


async def load_phones_with_prior_campaign_media(
    db: AsyncSession,
    *,
    campaign_media_id: int,
    exclude_run_id: int | None = None,
    include_pending_on_other_runs: bool = True,
) -> set[str]:
    """Normalized 10-digit keys that already received (or are queued for) this campaign media.

    Cross-instance: any Evolution line. Sources: outbound inbox rows + campaign run queues.
    """
    import json

    media_ids = await campaign_media_equivalent_ids(db, campaign_media_id)
    markers = _media_markers_for_ids(media_ids)
    keys: set[str] = set()

    # Outbound WhatsApp inbox (successful media sends)
    marker_clauses = [
        or_(
            WhatsAppMessage.media_name.startswith(marker + ":"),
            WhatsAppMessage.media_name == marker,
        )
        for marker in markers
    ]
    if marker_clauses:
        stmt = (
            select(WhatsAppConversation.customer_phone, WhatsAppMessage.status)
            .join(
                WhatsAppConversation,
                WhatsAppConversation.id == WhatsAppMessage.conversation_id,
            )
            .where(
                WhatsAppMessage.organization_id == ORG,
                WhatsAppMessage.direction == "out",
                WhatsAppMessage.media_type != "",
                WhatsAppMessage.media_type.is_not(None),
                or_(*marker_clauses),
            )
            .limit(50000)
        )
        for cphone, status in (await db.execute(stmt)).all():
            st = (status or "").strip().lower()
            if st == "failed":
                continue
            pk = phone_key(cphone)
            if pk:
                keys.add(pk)

    run_stmt = select(CampaignRun).where(CampaignRun.media_id.in_(list(media_ids)))
    runs = list((await db.execute(run_stmt)).scalars().all())
    for run in runs:
        if exclude_run_id is not None and int(run.id) == int(exclude_run_id):
            continue
        try:
            items = json.loads(run.recipients_json or "[]")
        except json.JSONDecodeError:
            items = []
        if not isinstance(items, list):
            continue
        run_status = (run.status or "").strip().lower()
        active = run_status in {"queued", "running"}
        for item in items:
            if not isinstance(item, dict):
                continue
            st = (item.get("status") or "").strip().lower()
            if st == "sent":
                pk = phone_key(str(item.get("to_phone") or ""))
                if pk:
                    keys.add(pk)
                continue
            if include_pending_on_other_runs and active and st == "pending":
                pk = phone_key(str(item.get("to_phone") or ""))
                if pk:
                    keys.add(pk)
    return keys


def filter_recipients_excluding_prior_campaign_media(
    recipients: list[dict[str, str]],
    *,
    prior_phone_keys: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Drop recipients whose phone already got this campaign media (any sender instance)."""
    if not prior_phone_keys:
        return recipients, []
    allowed: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    for rec in recipients:
        phone = str(rec.get("to_phone") or "")
        pk = phone_key(phone)
        if pk and pk in prior_phone_keys:
            excluded.append(
                {
                    "to_phone": phone,
                    "customer_name": rec.get("customer_name") or "",
                    "reason": "duplicate_prior_media",
                }
            )
            continue
        allowed.append(rec)
    return allowed, excluded


async def load_spam_phone_keys(db: AsyncSession) -> set[str]:
    """Normalized 10-digit keys for any WhatsApp conversation marked spam (any instance)."""
    keys: set[str] = set()
    rows = (
        await db.execute(
            select(WhatsAppConversation.customer_phone).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.is_spam.is_(True),
            )
        )
    ).all()
    for (cphone,) in rows:
        pk = phone_key(cphone)
        if pk and len(pk) >= 8:
            keys.add(pk)
    return keys


async def phone_marked_spam(db: AsyncSession, to_phone: str | None) -> bool:
    """True when any org conversation for this phone is marked spam.

    Matches by normalized IN mobile key so ``91…``, ``+91…``, and 10-digit forms align.
    Prefer this at send time so newly marked spam stops mid-campaign remaining queue.
    """
    key = phone_key(to_phone)
    if not key or len(key) < 8:
        return False
    raw = (to_phone or "").strip()
    rows = (
        await db.execute(
            select(WhatsAppConversation.customer_phone).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.is_spam.is_(True),
                or_(
                    WhatsAppConversation.customer_phone == key,
                    WhatsAppConversation.customer_phone == f"91{key}",
                    WhatsAppConversation.customer_phone == raw,
                    WhatsAppConversation.customer_phone.ilike(f"%{key}"),
                ),
            ).limit(40)
        )
    ).all()
    for (cphone,) in rows:
        if phone_key(cphone) == key:
            return True
    return False


def filter_recipients_excluding_spam(
    recipients: list[dict[str, str]],
    *,
    spam_phone_keys: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Drop recipients whose phone is on a spam-flagged WhatsApp conversation."""
    if not spam_phone_keys:
        return recipients, []
    allowed: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    for rec in recipients:
        phone = str(rec.get("to_phone") or "")
        pk = phone_key(phone)
        if pk and pk in spam_phone_keys:
            excluded.append(
                {
                    "to_phone": phone,
                    "customer_name": rec.get("customer_name") or "",
                    "reason": "spam",
                }
            )
            continue
        allowed.append(rec)
    return allowed, excluded


async def filter_recipients_for_campaign_media_send(
    db: AsyncSession,
    recipients: list[dict[str, str]],
    *,
    campaign_media_id: int,
    self_keys: set[str],
    staff_keys: set[str],
    exclude_run_id: int | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Self/staff/batch-dup filter, spam exclusion, then cross-campaign media dedupe."""
    allowed, excluded = filter_recipients_excluding_blocked(
        recipients, self_keys=self_keys, staff_keys=staff_keys
    )
    spam_keys = await load_spam_phone_keys(db)
    allowed, spam_excluded = filter_recipients_excluding_spam(
        allowed, spam_phone_keys=spam_keys
    )
    excluded.extend(spam_excluded)
    prior = await load_phones_with_prior_campaign_media(
        db,
        campaign_media_id=campaign_media_id,
        exclude_run_id=exclude_run_id,
    )
    allowed, media_excluded = filter_recipients_excluding_prior_campaign_media(
        allowed, prior_phone_keys=prior
    )
    excluded.extend(media_excluded)
    return allowed, excluded


def filter_recipients_excluding_blocked(
    recipients: list[dict[str, str]],
    *,
    self_keys: set[str],
    staff_keys: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return (allowed, excluded) where excluded have reason self|staff."""
    allowed: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    seen: set[str] = set()
    for rec in recipients:
        phone = str(rec.get("to_phone") or "")
        key = phone_key(phone)
        if key and key in seen:
            excluded.append(
                {
                    "to_phone": phone,
                    "customer_name": rec.get("customer_name") or "",
                    "reason": "duplicate_in_batch",
                }
            )
            continue
        if key:
            seen.add(key)
        kind = classify_blocked_recipient(phone, self_keys=self_keys, staff_keys=staff_keys)
        if kind:
            excluded.append(
                {
                    "to_phone": phone,
                    "customer_name": rec.get("customer_name") or "",
                    "reason": kind,
                }
            )
            continue
        allowed.append(rec)
    return allowed, excluded
