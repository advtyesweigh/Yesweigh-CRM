"""Persist Campaign Report rows from WhatsApp batch sends."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Campaign,
    CampaignMedia,
    CampaignRun,
    User,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppMessage,
)

logger = logging.getLogger(__name__)

ORG = "yesweigh"
# Public Campaign Report IDs (uid / campaign_number) start at 111.
CAMPAIGN_RUN_UID_START = 111


async def allocate_campaign_run_uid(db: AsyncSession) -> int:
    """Next free public campaign number starting at 111."""
    used = {
        int(v)
        for v in (
            await db.execute(select(CampaignRun.uid).where(CampaignRun.uid.is_not(None)))
        )
        .scalars()
        .all()
        if v is not None
    }
    candidate = CAMPAIGN_RUN_UID_START
    while candidate in used:
        candidate += 1
    return candidate


def _audience_label(args: dict[str, Any], result: dict[str, Any]) -> str:
    meta = result.get("filter_meta") if isinstance(result.get("filter_meta"), dict) else {}
    aud_name = str(
        meta.get("audience_name") or args.get("audience_name") or ""
    ).strip()
    uid_label = str(
        meta.get("audience_uid_label") or meta.get("audience_uid") or args.get("audience_uid") or ""
    ).strip()
    if uid_label and uid_label.isdigit() and 11 <= int(uid_label) <= 99:
        uid_label = f"{int(uid_label):02d}"
    else:
        uid_label = ""
    if aud_name and uid_label:
        return f"{aud_name} · {uid_label}"[:200]
    if aud_name:
        # audience_name may itself be a 2-digit uid reference
        if aud_name.isdigit() and 11 <= int(aud_name) <= 99 and not uid_label:
            return f"{int(aud_name):02d}"[:200]
        return aud_name[:200]
    if uid_label:
        return uid_label[:200]
    if meta.get("filter") == "audience" or args.get("audience_id") is not None or args.get("audience_uid") is not None:
        return "Custom audience"
    state = str(meta.get("state") or args.get("filter_state") or args.get("state") or "").strip()
    filt = str(
        args.get("recipient_filter") or args.get("filter") or meta.get("filter") or ""
    ).strip().lower()
    if state:
        return f"{state} dealers"
    if filt in {"kerala_dealers", "kerala", "all_kerala_dealers"}:
        return "Kerala dealers"
    if filt in {"dealers", "dealer", "state_dealers"}:
        return "Dealers"
    return "Custom recipients"


def format_media_display_name(
    media_row: CampaignMedia | None,
    *,
    fallback: str = "",
) -> str:
    """Campaign Report MEDIA NAME: media title + UID (e.g. 'GATC RAM · 1000')."""
    title = ""
    uid: int | None = None
    if media_row is not None:
        title = (media_row.title or media_row.file_name or "").strip()
        raw_uid = getattr(media_row, "uid", None)
        if raw_uid is not None:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                uid = None
    if not title:
        title = (fallback or "").strip()
        # Drop legacy "TITLE / TAG" suffixes (e.g. "GATC RAM / BENCH PRO").
        if " / " in title:
            title = title.split(" / ", 1)[0].strip()
    if title and uid is not None:
        return f"{title} · {uid}"[:240]
    if title:
        return title[:240]
    if uid is not None:
        return str(uid)[:240]
    return "—"


def _media_label(result: dict[str, Any], media_row: CampaignMedia | None) -> str:
    fallback = str(result.get("media_title") or "").strip()
    return format_media_display_name(media_row, fallback=fallback)


def _agent_label(result: dict[str, Any], user: User | None) -> str:
    for key in ("from_employee_name", "from_display_name"):
        val = str(result.get(key) or "").strip()
        if val:
            return val[:200]
    if user and (user.name or "").strip():
        return user.name.strip()[:200]
    return "AI Agent"


def _counts(result: dict[str, Any]) -> tuple[int, int, int, int]:
    success = int(result.get("sent_count") or 0)
    failed = int(result.get("failed_count") or 0)
    skipped = int(
        result.get("skipped_count")
        or result.get("skipped_duplicate_count")
        or 0
    )
    # Prefer explicit skipped_count; still count duplicate-only skips when skipped_count absent.
    if result.get("skipped_count") is None and result.get("skipped_duplicate_count") is not None:
        skipped = int(result.get("skipped_duplicate_count") or 0)
    total = int(result.get("recipient_count") or 0)
    if total <= 0:
        total = success + failed + skipped
    return total, success, failed, skipped


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _recipient_outbound_anchors(
    run: CampaignRun,
) -> tuple[dict[str, tuple[datetime, int | None]], dict[str, str]]:
    """phone_key → (sent_at, conversation_id) from recipients_json (sent rows with real sent_at only).

    Never falls back to run.started_at — that would count pre-send inbound as replies.

    Also returns phone_key → conversation public_id when recipients_json stored a UUID
    (inbox public_id) instead of the numeric PK.
    """
    from app.ai.whatsapp_send_guards import phone_key

    try:
        recipients = json.loads(run.recipients_json or "[]")
    except json.JSONDecodeError:
        recipients = []
    if not isinstance(recipients, list):
        return {}, {}

    out: dict[str, tuple[datetime, int | None]] = {}
    public_ids: dict[str, str] = {}
    for item in recipients:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"sent", "success"}:
            continue
        pk = phone_key(str(item.get("to_phone") or ""))
        sent_at = _parse_dt(item.get("sent_at"))
        if not pk or sent_at is None:
            continue
        cid_raw = item.get("conversation_id")
        cid: int | None = None
        public_id = ""
        if cid_raw is not None:
            raw = str(cid_raw).strip()
            if raw.isdigit():
                cid = int(raw)
            elif len(raw) >= 32:
                # Inbox stores conversation public_id (UUID) in recipients_json.
                public_id = raw[:80]
        prev = out.get(pk)
        if prev is None or sent_at < prev[0]:
            out[pk] = (sent_at, cid if cid is not None else (prev[1] if prev else None))
            if public_id:
                public_ids[pk] = public_id
        elif prev is not None and prev[1] is None and cid is not None:
            out[pk] = (prev[0], cid)
        elif pk not in public_ids and public_id:
            public_ids[pk] = public_id
    return out, public_ids


def _run_outbound_window(run: CampaignRun) -> tuple[datetime, datetime] | None:
    started = _parse_dt(run.started_at)
    finished = _parse_dt(run.finished_at)
    occurred = _parse_dt(run.occurred_at)
    # Legacy/restored report rows often only set occurred_at at batch end.
    if started is not None:
        window_start = started - timedelta(minutes=10)
    elif occurred is not None:
        window_start = occurred - timedelta(hours=3)
    else:
        return None
    status = (run.status or "").strip().lower()
    if finished is not None:
        window_end = finished + timedelta(minutes=30)
    elif status in {"completed", "failed", "stopped"} and occurred is not None:
        window_end = occurred + timedelta(minutes=30)
    else:
        window_end = _now() + timedelta(minutes=5)
    return window_start, window_end


async def _outbound_anchors_from_messages(
    db: AsyncSession, run: CampaignRun
) -> dict[str, tuple[datetime, int]]:
    """phone_key → (sent_at, conversation_id) from this run's campaign media outbounds.

    Uses the earliest outbound of this media in the run window (same conversation).

    Scoped to the run's sending WhatsApp instance when ``from_instance_public_id``
    is set — otherwise parallel campaigns that share the same media (e.g. Visakh
    112 + Biju 113 both sending GATC RAM) cross-contaminate replied lists.
    """
    from app.ai.whatsapp_send_guards import phone_key

    media_id = run.media_id
    if media_id is None:
        return {}
    window = _run_outbound_window(run)
    if window is None:
        return {}
    window_start, window_end = window
    marker = f"campaign_media:{int(media_id)}"
    from_inst = (run.from_instance_public_id or "").strip()
    stmt = (
        select(
            WhatsAppMessage.created_at,
            WhatsAppMessage.conversation_id,
            WhatsAppConversation.customer_phone,
        )
        .join(
            WhatsAppConversation,
            WhatsAppConversation.id == WhatsAppMessage.conversation_id,
        )
        .where(
            WhatsAppMessage.organization_id == ORG,
            WhatsAppMessage.direction == "out",
            WhatsAppMessage.created_at >= window_start,
            WhatsAppMessage.created_at <= window_end,
            or_(
                WhatsAppMessage.media_name.startswith(marker + ":"),
                WhatsAppMessage.media_name == marker,
            ),
        )
    )
    if from_inst:
        stmt = stmt.join(
            WhatsAppInstance,
            WhatsAppInstance.id == WhatsAppConversation.whatsapp_instance_id,
        ).where(WhatsAppInstance.public_id == from_inst)
    rows = (await db.execute(stmt)).all()
    out: dict[str, tuple[datetime, int]] = {}
    for created_at, conversation_id, cphone in rows:
        pk = phone_key(cphone)
        sent_at = _parse_dt(created_at)
        if not pk or sent_at is None or conversation_id is None:
            continue
        cid = int(conversation_id)
        prev = out.get(pk)
        if prev is None or sent_at < prev[0]:
            out[pk] = (sent_at, cid)
    return out


async def _sent_phone_map_from_messages(db: AsyncSession, run: CampaignRun) -> dict[str, datetime]:
    """Compatibility helper: phone_key → outbound sent_at from inbox messages."""
    anchors = await _outbound_anchors_from_messages(db, run)
    return {pk: ts for pk, (ts, _cid) in anchors.items()}


async def _campaign_run_replied_phone_convs(
    db: AsyncSession, run: CampaignRun
) -> dict[str, int]:
    """phone_key → conversation_id for recipients who replied after this run's outbound.

    Pre-send inbound does not count. Only `direction=in` with `created_at` strictly
    after that recipient's outbound campaign media `created_at`.
    """
    # Prefer inbox outbounds (precise conversation_id + timestamp). Merge recipient
    # anchors only when they have a real per-send sent_at (never run.started_at).
    anchors: dict[str, tuple[datetime, int | None]] = {}
    if run.media_id is not None and int(run.success_count or 0) > 0:
        for pk, pair in (await _outbound_anchors_from_messages(db, run)).items():
            anchors[pk] = pair
    recip_anchors, recip_public_ids = _recipient_outbound_anchors(run)
    for pk, pair in recip_anchors.items():
        if pk not in anchors:
            anchors[pk] = pair
        elif anchors[pk][1] is None and pair[1] is not None:
            anchors[pk] = (anchors[pk][0], pair[1])
    if not anchors:
        return {}

    from app.ai.whatsapp_send_guards import phone_key

    # Resolve conversation public_ids stored in recipients_json → numeric PKs.
    need_public = {
        pk: pid
        for pk, pid in recip_public_ids.items()
        if anchors.get(pk, (None, None))[1] is None and pid
    }
    if need_public:
        rows = (
            await db.execute(
                select(WhatsAppConversation.id, WhatsAppConversation.public_id).where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.public_id.in_(list(need_public.values())),
                )
            )
        ).all()
        by_public = {str(pid): int(cid) for cid, pid in rows if cid is not None and pid}
        for pk, pid in need_public.items():
            resolved = by_public.get(pid)
            if resolved is None:
                continue
            sent_at, _ = anchors[pk]
            anchors[pk] = (sent_at, resolved)

    # Resolve missing conversation_ids via phone → conversation (exact 10-digit key).
    # Prefer the campaign's sending instance when known (avoids other-agent chats).
    need_phone = [pk for pk, (_ts, cid) in anchors.items() if cid is None]
    phone_to_convs: dict[str, list[int]] = {pk: [] for pk in need_phone}
    from_inst = (run.from_instance_public_id or "").strip()
    sender_instance_id: int | None = None
    if from_inst and need_phone:
        sender_instance_id = (
            await db.execute(
                select(WhatsAppInstance.id).where(WhatsAppInstance.public_id == from_inst)
            )
        ).scalar_one_or_none()

    for i in range(0, len(need_phone), 40):
        chunk = need_phone[i : i + 40]
        clauses = [WhatsAppConversation.customer_phone.ilike(f"%{k}") for k in chunk]
        stmt = select(
            WhatsAppConversation.id,
            WhatsAppConversation.customer_phone,
            WhatsAppConversation.whatsapp_instance_id,
        ).where(
            WhatsAppConversation.organization_id == ORG,
            or_(*clauses),
        )
        rows = (await db.execute(stmt)).all()
        # Prefer sender-instance matches; fall back to any instance only if none.
        preferred: dict[str, list[int]] = {pk: [] for pk in chunk}
        fallback: dict[str, list[int]] = {pk: [] for pk in chunk}
        for cid, cphone, inst_id in rows:
            pk = phone_key(cphone)
            if pk not in preferred:
                continue
            n = int(cid)
            if sender_instance_id is not None and int(inst_id or 0) == int(sender_instance_id):
                preferred[pk].append(n)
            else:
                fallback[pk].append(n)
        for pk in chunk:
            phone_to_convs[pk] = preferred[pk] or fallback[pk]

    # phone_key → list of (sent_at, conversation_id) to check
    checks: list[tuple[str, datetime, int]] = []
    for pk, (sent_at, cid) in anchors.items():
        if cid is not None:
            checks.append((pk, sent_at, cid))
        else:
            for resolved in phone_to_convs.get(pk, []):
                checks.append((pk, sent_at, resolved))
    if not checks:
        return {}

    all_conv_ids = sorted({cid for _pk, _ts, cid in checks})
    earliest = min(ts for _pk, ts, _cid in checks)
    inbound_rows = (
        await db.execute(
            select(WhatsAppMessage.conversation_id, WhatsAppMessage.created_at)
            .where(
                WhatsAppMessage.conversation_id.in_(all_conv_ids),
                WhatsAppMessage.direction == "in",
                WhatsAppMessage.created_at > earliest,
            )
            .order_by(WhatsAppMessage.created_at.asc())
        )
    ).all()
    inbound_by_conv: dict[int, list[datetime]] = {}
    for cid, created_at in inbound_rows:
        dt = _parse_dt(created_at)
        if dt is None:
            continue
        inbound_by_conv.setdefault(int(cid), []).append(dt)

    replied: dict[str, int] = {}
    for pk, sent_at, cid in checks:
        if pk in replied:
            continue
        # Strictly after THIS recipient's outbound — ignore anything before the send.
        if any(inbound_at > sent_at for inbound_at in inbound_by_conv.get(cid, [])):
            replied[pk] = cid
    return replied


async def compute_campaign_run_replied_count(db: AsyncSession, run: CampaignRun) -> int:
    """Count recipients with inbound WhatsApp after this run's outbound media."""
    return len(await _campaign_run_replied_phone_convs(db, run))


async def list_campaign_run_replied_conversation_ids(
    db: AsyncSession, run: CampaignRun
) -> set[int]:
    """Conversation PKs that replied after this campaign's media send (Report logic)."""
    return set((await _campaign_run_replied_phone_convs(db, run)).values())


async def find_campaign_run_by_uid(db: AsyncSession, uid: int) -> CampaignRun | None:
    return (
        await db.execute(select(CampaignRun).where(CampaignRun.uid == int(uid)))
    ).scalar_one_or_none()


def _campaign_filter_hint(run: CampaignRun) -> str:
    """Short sender · media · audience label for inbox campaign pills."""
    agent = (run.agent_name or "").strip()
    if agent:
        # First token is enough in the narrow inbox select (Visakh B → Visakh).
        agent = agent.split()[0]
    media = (run.media_name or "").strip()
    if " · " in media:
        media = media.split(" · ", 1)[0].strip()
    aud = (run.audience_name or "").strip()
    if " · " in aud:
        aud = aud.split(" · ", 1)[0].strip()
    parts = [p for p in (agent, media, aud) if p]
    if parts:
        return " · ".join(parts)[:56]
    return ""


async def list_campaign_reply_filters(db: AsyncSession) -> list[dict[str, Any]]:
    """Campaigns with replies — for WhatsApp Inbox All + campaign ID pills."""
    rows = (
        await db.execute(
            select(CampaignRun)
            .where(CampaignRun.uid.is_not(None))
            .where(CampaignRun.replied_count > 0)
            .order_by(CampaignRun.uid.desc())
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        uid = int(r.uid) if r.uid is not None else None
        if uid is None:
            continue
        replied = int(r.replied_count or 0)
        if replied <= 0:
            continue
        hint = _campaign_filter_hint(r)
        out.append(
            {
                "uid": uid,
                "campaign_number": uid,
                "replied_count": replied,
                "media_name": (r.media_name or "")[:240],
                "audience_name": (r.audience_name or "")[:200],
                "agent_name": (r.agent_name or "")[:200],
                "hint": hint,
                "label": str(uid),
                "status": (r.status or "completed")[:40],
            }
        )
    return out


async def refresh_campaign_run_replied_count(db: AsyncSession, run: CampaignRun | None) -> int:
    """Recompute and cache replied_count on the campaign_runs row."""
    if run is None:
        return 0
    try:
        count = await compute_campaign_run_replied_count(db, run)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to compute replied_count for campaign_run %s", getattr(run, "id", None))
        return int(getattr(run, "replied_count", 0) or 0)
    run.replied_count = max(0, int(count))
    return run.replied_count


async def backfill_campaign_run_replied_counts(
    db: AsyncSession,
    *,
    run_ids: list[int] | None = None,
) -> list[tuple[int, int, int]]:
    """Recompute replied_count for selected runs (or all recent). Returns (id, old, new)."""
    q = select(CampaignRun).order_by(CampaignRun.id.asc())
    if run_ids:
        q = q.where(CampaignRun.id.in_(run_ids))
    rows = (await db.execute(q)).scalars().all()
    changes: list[tuple[int, int, int]] = []
    for run in rows:
        old = int(getattr(run, "replied_count", 0) or 0)
        new = await refresh_campaign_run_replied_count(db, run)
        changes.append((int(run.id), old, int(new)))
    await db.commit()
    return changes


async def _link_campaign_id(db: AsyncSession, media_id: int | None) -> int | None:
    if media_id is None:
        return None
    camps = (await db.execute(select(Campaign).order_by(Campaign.id.desc()).limit(40))).scalars().all()
    for c in camps:
        raw = c.media_ids or "[]"
        if f"[{media_id}]" in raw.replace(" ", "") or f",{media_id}," in f",{raw.strip('[]')},":
            return c.id
        try:
            import json

            ids = json.loads(raw)
            if isinstance(ids, list) and media_id in {int(x) for x in ids if str(x).isdigit()}:
                return c.id
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


async def start_campaign_run(
    db: AsyncSession,
    *,
    user: User | None,
    batch_id: str,
    tool_name: str,
    agent_name: str,
    media_name: str,
    media_id: int | None,
    audience_name: str,
    total_count: int,
    from_instance_public_id: str = "",
    note: str = "",
    total_hours: int = 0,
    status: str = "running",
) -> CampaignRun | None:
    """Insert (or reuse) a Campaign Report row as soon as a confirmed send starts."""
    bid = (batch_id or "").strip()[:80]
    if bid:
        existing = (
            await db.execute(select(CampaignRun).where(CampaignRun.batch_id == bid))
        ).scalar_one_or_none()
        if existing:
            return existing

    campaign_id = await _link_campaign_id(db, media_id)
    occurred = _now()
    run_uid = await allocate_campaign_run_uid(db)
    row = CampaignRun(
        campaign_id=campaign_id,
        uid=run_uid,
        batch_id=bid,
        tool_name=(tool_name or "")[:80],
        agent_name=(agent_name or _agent_label({}, user))[:200],
        media_name=(media_name or "—")[:240],
        media_id=media_id,
        audience_name=(audience_name or "")[:200],
        total_count=max(0, int(total_count or 0)),
        success_count=0,
        failed_count=0,
        skipped_count=0,
        replied_count=0,
        from_instance_public_id=(from_instance_public_id or "")[:80],
        note=(note or "Campaign started — sending…")[:4000],
        occurred_at=occurred,
        status=(status or "running")[:40],
        total_hours=int(total_hours or 0),
        started_at=occurred,
        finished_at=None,
        user_id=user.id if user else None,
    )
    db.add(row)
    try:
        await db.flush()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start campaign run report")
        return None
    return row


async def update_campaign_run_progress(
    db: AsyncSession,
    run: CampaignRun | None,
    *,
    success_count: int,
    failed_count: int,
    skipped_count: int,
    note: str | None = None,
    status: str = "running",
    refresh_replied: bool = False,
) -> None:
    """Bump success/failed/skipped while a batch is in flight."""
    if run is None:
        return
    run.success_count = max(0, int(success_count or 0))
    run.failed_count = max(0, int(failed_count or 0))
    run.skipped_count = max(0, int(skipped_count or 0))
    run.status = (status or "running")[:40]
    if note is not None:
        run.note = str(note)[:4000]
    if refresh_replied:
        await refresh_campaign_run_replied_count(db, run)


async def finish_campaign_run(
    db: AsyncSession,
    run: CampaignRun | None,
    *,
    success_count: int,
    failed_count: int,
    skipped_count: int,
    note: str = "",
    status: str = "completed",
) -> None:
    """Mark a Campaign Report row finished (completed/failed/stopped)."""
    if run is None:
        return
    run.success_count = max(0, int(success_count or 0))
    run.failed_count = max(0, int(failed_count or 0))
    run.skipped_count = max(0, int(skipped_count or 0))
    run.status = (status or "completed")[:40]
    if note:
        run.note = str(note)[:4000]
    run.finished_at = _now()
    await refresh_campaign_run_replied_count(db, run)


async def record_campaign_run_from_batch(
    db: AsyncSession,
    *,
    user: User | None,
    args: dict[str, Any],
    result: dict[str, Any],
    tool_name: str,
) -> CampaignRun | None:
    """Insert or finalize a Campaign Report row after a confirmed (non-dry-run) batch."""
    if not isinstance(result, dict):
        return None
    if result.get("dry_run"):
        return None
    if result.get("needs_confirmation"):
        return None

    batch_id = str(result.get("batch_id") or args.get("batch_id") or "").strip()
    existing: CampaignRun | None = None
    if batch_id:
        existing = (
            await db.execute(select(CampaignRun).where(CampaignRun.batch_id == batch_id))
        ).scalar_one_or_none()

    # Paced jobs already create a CampaignRun at schedule time — keep that row.
    if result.get("paced") or result.get("scheduled"):
        return existing

    total, success, failed_n, skipped = _counts(result)
    # Prefer skipped_count from result when present (includes staff exclusions etc.).
    if result.get("skipped_count") is not None:
        try:
            skipped = int(result.get("skipped_count") or 0)
        except (TypeError, ValueError):
            pass

    # Finalize an in-progress row created at send-start.
    if existing is not None:
        st = (existing.status or "").strip().lower()
        if st in {"running", "queued"} or (
            int(existing.success_count or 0)
            + int(existing.failed_count or 0)
            + int(existing.skipped_count or 0)
            == 0
            and total > 0
        ):
            final_status = "failed" if result.get("ok") is False and success <= 0 else "completed"
            if result.get("stopped_early"):
                final_status = "stopped"
            await finish_campaign_run(
                db,
                existing,
                success_count=success,
                failed_count=failed_n,
                skipped_count=skipped,
                note=str(result.get("note") or existing.note or ""),
                status=final_status,
            )
            if total > 0:
                existing.total_count = total
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to finalize campaign run report")
            return existing
        return existing

    # Only record when the batch actually attempted work
    sent = int(result.get("sent_count") or 0)
    failed = int(result.get("failed_count") or 0)
    skipped_dup = int(result.get("skipped_duplicate_count") or result.get("skipped_count") or 0)
    if sent + failed + skipped_dup <= 0 and not result.get("sent"):
        return None

    media_id_raw = result.get("media_id") or args.get("media_id")
    media_id: int | None = None
    try:
        if media_id_raw is not None and str(media_id_raw).strip():
            media_id = int(media_id_raw)
    except (TypeError, ValueError):
        media_id = None

    media_row: CampaignMedia | None = None
    if media_id is not None:
        media_row = await db.get(CampaignMedia, media_id)

    campaign_id = await _link_campaign_id(db, media_id)
    media_display = str(result.get("media_name") or "").strip() or _media_label(result, media_row)
    occurred = _now()
    run_uid = await allocate_campaign_run_uid(db)
    row = CampaignRun(
        campaign_id=campaign_id,
        uid=run_uid,
        batch_id=batch_id[:80],
        tool_name=(tool_name or "")[:80],
        agent_name=_agent_label(result, user),
        media_name=media_display[:240],
        media_id=media_id,
        audience_name=_audience_label(args, result)[:200],
        total_count=total,
        success_count=success,
        failed_count=failed_n,
        skipped_count=skipped,
        replied_count=0,
        from_instance_public_id=str(result.get("from_instance_public_id") or "")[:80],
        note=str(result.get("note") or "")[:4000],
        occurred_at=occurred,
        status="completed",
        started_at=occurred,
        finished_at=occurred,
        user_id=user.id if user else None,
    )
    db.add(row)
    try:
        await db.flush()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist campaign run report")
        return None
    return row
