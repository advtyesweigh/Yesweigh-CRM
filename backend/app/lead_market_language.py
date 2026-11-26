"""Lead business-language markets — state-inferred totals + optional phone index."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contact_spoken_language import normalize_spoken_language_code
from app.lead_state_market import (
    market_language_for_canon_state,
    state_match_values_for_market,
)
from app.models import Lead, WhatsAppConversation, WhatsAppMessage
from app.phone_util import normalize_in_mobile
from app.state_normalize import lead_state_bucket

VOICE_TYPES = ("audio", "ptt", "voice")

# Ordered for UI — south first (ml–te), pan-India (hi–od), then as / en / other (5×3 grid).
MARKET_LANGUAGE_TILES: list[tuple[str, str, str]] = [
    ("ml", "ml-IN", "Malayalam"),
    ("ta", "ta-IN", "Tamil"),
    ("kn", "kn-IN", "Kannada"),
    ("te", "te-IN", "Telugu"),
    ("hi", "hi-IN", "Hindi"),
    ("mr", "mr-IN", "Marathi"),
    ("gu", "gu-IN", "Gujarati"),
    ("bn", "bn-IN", "Bengali"),
    ("pa", "pa-IN", "Punjabi"),
    ("od", "od-IN", "Odia"),
    ("as", "as-IN", "Assamese"),
    ("en", "en-IN", "English"),
    ("other", "other", "Common"),
]

_MARKET_CODES = {code for code, _, _ in MARKET_LANGUAGE_TILES}
_KNOWN_MARKETS = {c for c in _MARKET_CODES if c not in ("other", "")}


def normalize_market_language(raw: str | None) -> str:
    """Map stored contact/spoken language to a market bucket code."""
    code = normalize_spoken_language_code(raw)
    if code in ("or", "ori"):
        return "od"
    if code in _MARKET_CODES:
        return code
    if not code:
        return ""
    return "other"


def market_for_stored_language(raw: str | None) -> str:
    """Non-empty stored language → market bucket (never blank)."""
    code = normalize_spoken_language_code(raw)
    if not code:
        return ""
    normalized = normalize_market_language(code)
    return normalized or "other"


def parse_language_filter(raw: str | None) -> str:
    """API `language=` → market bucket (ml-IN, hindi, …)."""
    token = (raw or "").strip().lower().replace("_", "-")
    if not token:
        return ""
    if token in ("other", "others", "unknown"):
        return "other"
    base = token.split("-", 1)[0]
    for candidate in (token, base):
        norm = normalize_market_language(candidate)
        if norm in _MARKET_CODES:
            return norm
    if base in _MARKET_CODES:
        return base
    return ""


@dataclass(frozen=True)
class LanguagePhoneIndex:
    """Latest WhatsApp contact language per 10-digit mobile."""

    by_phone: dict[str, str]

    def market_for_phone(self, phone: str | None) -> str:
        phone10 = normalize_in_mobile(phone)
        if not phone10:
            return ""
        raw = self.by_phone.get(phone10, "")
        if not raw:
            return ""
        return market_for_stored_language(raw)


async def build_language_phone_index(db: AsyncSession) -> LanguagePhoneIndex:
    """One language per phone — prefer conversation.language, else latest inbound spoken_language."""
    by_phone: dict[str, str] = {}

    conv_rows = (
        await db.execute(
            select(
                WhatsAppConversation.customer_phone,
                WhatsAppConversation.language,
                WhatsAppConversation.last_message_at,
            )
            .where(func.trim(WhatsAppConversation.language) != "")
            .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
        )
    ).all()

    for phone, language, _at in conv_rows:
        phone10 = normalize_in_mobile(phone)
        if not phone10 or phone10 in by_phone:
            continue
        lang = (language or "").strip()
        if lang:
            by_phone[phone10] = lang

    msg_rows = (
        await db.execute(
            select(
                WhatsAppConversation.customer_phone,
                WhatsAppMessage.spoken_language,
                WhatsAppMessage.created_at,
            )
            .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
            .where(WhatsAppMessage.direction == "in")
            .where(WhatsAppMessage.media_type.in_(VOICE_TYPES))
            .where(func.trim(WhatsAppMessage.spoken_language) != "")
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(50000)
        )
    ).all()

    for phone, spoken, _at in msg_rows:
        phone10 = normalize_in_mobile(phone)
        if not phone10 or phone10 in by_phone:
            continue
        by_phone[phone10] = (spoken or "").strip()

    return LanguagePhoneIndex(by_phone=by_phone)


async def lead_market_buckets(
    db: AsyncSession, *, status: str = ""
) -> tuple[dict[str, int], int, int]:
    """Count leads per market language from Lead.state; returns (buckets, total_leads, in_market)."""
    stmt = select(Lead.state, func.count()).group_by(Lead.state)
    st = (status or "").strip().lower()
    if st and st != "all":
        stmt = stmt.where(func.lower(Lead.status) == st)
    rows = (await db.execute(stmt)).all()

    buckets: dict[str, int] = {code: 0 for code, _, _ in MARKET_LANGUAGE_TILES}
    total = 0
    in_market = 0
    for state_raw, n in rows:
        count = int(n or 0)
        total += count
        canon = lead_state_bucket(str(state_raw or ""))
        market = market_language_for_canon_state(canon) if canon else ""
        if market and market in buckets and market != "other":
            in_market += count
            buckets[market] = buckets.get(market, 0) + count
        else:
            buckets["other"] = buckets.get("other", 0) + count

    return buckets, total, in_market


def phones_matching_market(index: LanguagePhoneIndex, market: str) -> set[str]:
    if market == "other":
        out: set[str] = set()
        for phone10, raw in index.by_phone.items():
            bucket = market_for_stored_language(raw)
            if bucket == "other" or bucket not in _KNOWN_MARKETS:
                out.add(phone10)
        return out
    return {
        phone10
        for phone10, raw in index.by_phone.items()
        if market_for_stored_language(raw) == market
    }


def _assigned_market_state_match_values() -> list[str]:
    """Lead.state spellings for all state-inferred markets (excludes other / en-only)."""
    seen: set[str] = set()
    out: list[str] = []
    for code in _KNOWN_MARKETS:
        if code in ("other", "en"):
            continue
        for v in state_match_values_for_market(code):
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


async def apply_lead_language_filter(stmt, db: AsyncSession, language: str):
    """Restrict a Lead select to rows in states mapped to the given language market."""
    _ = db  # kept for call-site compatibility
    market = parse_language_filter(language)
    if not market:
        return stmt.where(Lead.id < 0)

    if market == "other":
        assigned = _assigned_market_state_match_values()
        if not assigned:
            return stmt
        assigned_clause = or_(*[Lead.state.ilike(v) for v in assigned])
        return stmt.where(
            or_(
                Lead.state.is_(None),
                func.trim(Lead.state) == "",
                ~assigned_clause,
            )
        )

    state_vals = state_match_values_for_market(market)
    if not state_vals:
        return stmt.where(Lead.id < 0)
    clauses = [Lead.state.ilike(v) for v in state_vals]
    return stmt.where(or_(*clauses))