"""Persistent AI learned knowledge (aliases, abbreviations, short names, facts).

Stored in VPS Postgres `ai_knowledge`. Loaded into the system prompt each turn;
Gemini can also call `remember_fact` / `search_knowledge`. Light regex auto-extract
picks up teach phrases from user chat ("MSB means …", "X is short for Y").
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiKnowledge

ORG = "yesweigh"

KINDS = frozenset(
    {"alias", "abbreviation", "report_type", "machine_short_name", "fact"}
)
SOURCES = frozenset({"chat", "manual", "tool"})

# Cap prompt injection so knowledge never blows the context window.
_PROMPT_LIMIT = 80
_KEY_MAX = 120
_VALUE_MAX = 2000
_NOTES_MAX = 500

# Teach / glossary patterns (user message → key/value). Case-insensitive.
_EXTRACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "MSB means Machine Short Name" / "MSB = Machine Short Name"
    (
        re.compile(
            r"^\s*(?:remember(?:\s+that)?[:\s]+)?([A-Za-z][A-Za-z0-9._/-]{0,40})\s+"
            r"(?:means|stands\s+for|=)\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "abbreviation",
    ),
    # "MSB is the short name for …" / "MSB is short for …"
    (
        re.compile(
            r"^\s*(?:remember(?:\s+that)?[:\s]+)?([A-Za-z][A-Za-z0-9._/-]{0,40})\s+"
            r"is\s+(?:the\s+)?short\s+(?:name|form)\s+(?:for|of)\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "machine_short_name",
    ),
    # "short name for X is Y" / "short name of X = Y"
    (
        re.compile(
            r"^\s*(?:the\s+)?short\s+name\s+(?:for|of)\s+(.+?)\s+(?:is|=)\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "machine_short_name",
    ),
    # "abbreviation for X is Y"
    (
        re.compile(
            r"^\s*(?:the\s+)?abbreviation\s+(?:for|of)\s+(.+?)\s+(?:is|=)\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "abbreviation",
    ),
    # "alias for X is Y" / "X is an alias for Y"
    (
        re.compile(
            r"^\s*(?:the\s+)?alias\s+(?:for|of)\s+(.+?)\s+(?:is|=)\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "alias",
    ),
    (
        re.compile(
            r"^\s*([A-Za-z][A-Za-z0-9._/-]{0,40})\s+is\s+an?\s+alias\s+for\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "alias",
    ),
    # "report type X is Y" / "X is a report type for Y"
    (
        re.compile(
            r"^\s*(?:the\s+)?report\s+type\s+([A-Za-z][A-Za-z0-9._/-]{0,40})\s+(?:is|=|means)\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "report_type",
    ),
    # "remember: key = value" / "learn that key is value" (generic fact)
    (
        re.compile(
            r"^\s*(?:remember|learn|note)(?:\s+that)?[:\s]+(.+?)\s+(?:is|=|means)\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "fact",
    ),
    # Bare "MSB is Machine Short Name" (short key, longer value) — after more specific patterns
    (
        re.compile(
            r"^\s*([A-Za-z][A-Za-z0-9._/-]{1,20})\s+is\s+(.+?)\s*$",
            re.I | re.S,
        ),
        "abbreviation",
    ),
    # "teach ai dealer = weighing scale dealer" / "dealer = weighing scale related only"
    (
        re.compile(
            r"^\s*(?:teach\s+ai\s+)?dealer\s*=\s*(.+?)\s*$",
            re.I | re.S,
        ),
        "fact",
    ),
]

# Org-wide standing rules (upserted on migrate — always in system prompt via ai_knowledge).
BUILTIN_KNOWLEDGE: list[dict[str, str]] = [
    {
        "kind": "fact",
        "key": "dealer",
        "value": (
            "Always means weighing scale dealer in YesWeigh CRM. Google Places / Dealer Finder "
            "use DEFAULT_CATEGORIES (7 weighing-scale queries). Never generic or other-product dealers."
        ),
        "notes": "Standing rule — AI Agent + Dealer Finder; do not search unrelated Places categories",
        "source": "manual",
    },
    {
        "kind": "alias",
        "key": "dealers",
        "value": "weighing scale dealers (Google / Dealer Finder discovery)",
        "notes": "Same hard rule as dealer",
        "source": "manual",
    },
    {
        "kind": "fact",
        "key": "dealer_finder_list_exclusions",
        "value": (
            "When the user asks to remove/exclude/drop a district, city, or keyword from a "
            "Dealer Finder final phone-NEW list, call filter_dealer_finder_results or "
            "get_dealer_finder_results with exclude_districts / exclude_cities / exclude_keywords "
            "(case-insensitive; keywords match name, city, or district contains). "
            "Do NOT re-run start_dealer_finder. Refresh the chat card with the new unique phone NEW "
            "count. Pass the same exclude_* args on import_dealer_finder_leads after user confirms."
        ),
        "notes": "List hygiene after statewide scan — no code deploy per filter",
        "source": "manual",
    },
    {
        "kind": "fact",
        "key": "dealer_finder_mobile_only",
        "value": (
            "Dealer Finder phone-NEW lists are mobile-only: require a phone and classify "
            "Indian numbers via phone_util (10-digit starting 6–9 after +91/0 normalization; "
            "drop landlines such as 0484… STD and numbers without phone). "
            "When the user asks to remove landlines or keep only mobile/WhatsApp numbers, "
            "call filter_dealer_finder_results or get_dealer_finder_results with mobile_only=true "
            "(no re-scan). Refresh the chat card with the new unique mobile NEW count."
        ),
        "notes": "Shared phone_filters helper — list + import with_phone_only",
        "source": "manual",
    },
    {
        "kind": "alias",
        "key": "pondicherry",
        "value": "Puducherry (UT) — Dealer Finder state scope",
        "notes": "Spelling alias for statewide jobs",
        "source": "manual",
    },
    {
        "kind": "fact",
        "key": "custom_audience_from_leads",
        "value": (
            "Custom Audience create/new from CRM Leads (Audience page /assets) uses create_audience "
            "with source=leads and filters.state — NEVER start_dealer_finder or Google Places statewide "
            "dealer scan. Phrases: create audience … from leads, not from dealer, state = X|Y. "
            "Tamil Nadu / Puducherry in the audience command is a CRM Leads filter, not Dealer Finder."
        ),
        "notes": "Prevents TN/PY audience loop into Dealer Finder confirm",
        "source": "manual",
    },
    {
        "kind": "fact",
        "key": "whatsapp_dealer_enquiry",
        "value": (
            "WhatsApp dealer enquiry = customer asks dealer/wholesale/distributor price, dealer model list, "
            "or becoming a dealer (still Sales service_category, enquiry_subtype dealer enquiry). "
            "Auto-upsert CRM Lead (source WhatsApp, tag Dealer enquiry) unless phone is an existing Dealer. "
            "Sales attended = outbound on Evolution lines (Biju/Visakh/Shibin), not Care handoff alone. "
            "Unattended: High priority, call with dealer prices, assign sales line."
        ),
        "notes": "Care + sales inbox — leads + cross-instance attendance",
        "source": "manual",
    },
    {
        "kind": "fact",
        "key": "puducherry_dealer_finder_scope",
        "value": (
            "Pondicherry / Puducherry dealer or Google Places discovery is strictly state=Puducherry "
            "(union territory). Do not mix Kerala or Tamil Nadu CRM/Places rows into that job or list. "
            "Districts include Karaikal, Mahe, Puducherry, Yanam; user may exclude Mahe (or others) "
            "from the final phone-NEW table via list exclusions without cancelling the scan."
        ),
        "notes": "Standing scope for Puducherry UT dealer finder tasks",
        "source": "manual",
    },
]


def normalize_key(key: str) -> str:
    return re.sub(r"\s+", " ", (key or "").strip())[:_KEY_MAX]


def normalize_kind(kind: str | None) -> str:
    k = (kind or "fact").strip().lower().replace("-", "_").replace(" ", "_")
    if k in {"short_name", "machine_short", "machine", "msn"}:
        k = "machine_short_name"
    if k in {"abbr", "abbrev"}:
        k = "abbreviation"
    if k in {"report", "reporttype"}:
        k = "report_type"
    if k not in KINDS:
        return "fact"
    return k


def normalize_source(source: str | None) -> str:
    s = (source or "tool").strip().lower()
    return s if s in SOURCES else "tool"


def _guess_kind_from_key_value(key: str, value: str, kind: str) -> str:
    """Refine 'abbreviation' guess when key looks like a product/machine code."""
    if kind != "abbreviation":
        return kind
    k = key.strip()
    v = value.strip().lower()
    if "short name" in v or "machine" in v:
        return "machine_short_name"
    if "report" in v:
        return "report_type"
    # All-caps short tokens (MSB, Q9LBL) often machine/product short names
    if len(k) <= 12 and k.upper() == k and k.isalpha():
        return "machine_short_name"
    return kind


def extract_teachings(message: str) -> list[dict[str, str]]:
    """Light regex extract of teach phrases from a user message. Empty if none."""
    text = (message or "").strip()
    if not text or len(text) > 800:
        return []
    # Prefer first non-empty line / whole message
    candidates = [text]
    if "\n" in text:
        candidates = [ln.strip() for ln in text.splitlines() if ln.strip()] + [text]

    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in candidates:
        for pat, default_kind in _EXTRACT_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            raw_key, raw_value = m.group(1), m.group(2)
            # Pattern "short name for X is Y" → key=Y (short), value=X (full)
            if default_kind == "machine_short_name" and pat.pattern.startswith(
                r"^\s*(?:the\s+)?short\s+name"
            ):
                raw_key, raw_value = m.group(2), m.group(1)
            if default_kind == "abbreviation" and "abbreviation" in pat.pattern:
                raw_key, raw_value = m.group(2), m.group(1)
            if default_kind == "alias" and pat.pattern.startswith(r"^\s*(?:the\s+)?alias"):
                raw_key, raw_value = m.group(2), m.group(1)

            key = normalize_key(raw_key)
            value = re.sub(r"\s+", " ", (raw_value or "").strip())[:_VALUE_MAX]
            if not key or not value or key.lower() == value.lower():
                continue
            # Skip conversational false positives ("this is great", "what is sales")
            if key.lower() in {
                "this",
                "that",
                "it",
                "what",
                "who",
                "there",
                "here",
                "sales",
                "total",
                "my",
                "your",
                "the",
            }:
                continue
            if len(value) < 2:
                continue
            kind = _guess_kind_from_key_value(key, value, default_kind)
            sig = (kind, key.lower())
            if sig in seen:
                continue
            seen.add(sig)
            found.append({"kind": kind, "key": key, "value": value})
            break  # one pattern per line
        if found and line is text:
            break
    return found


async def upsert_knowledge(
    db: AsyncSession,
    *,
    kind: str,
    key: str,
    value: str,
    notes: str = "",
    source: str = "tool",
    session_id: int | None = None,
    run_id: int | None = None,
    organization_id: str = ORG,
) -> AiKnowledge:
    kind_n = normalize_kind(kind)
    key_n = normalize_key(key)
    value_n = re.sub(r"\s+", " ", (value or "").strip())[:_VALUE_MAX]
    notes_n = (notes or "").strip()[:_NOTES_MAX]
    source_n = normalize_source(source)
    if not key_n or not value_n:
        raise ValueError("key and value are required")

    existing = (
        await db.execute(
            select(AiKnowledge).where(
                AiKnowledge.organization_id == organization_id,
                AiKnowledge.kind == kind_n,
                func.lower(AiKnowledge.key) == key_n.lower(),
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.value = value_n
        if notes_n:
            existing.notes = notes_n
        existing.source = source_n
        if session_id is not None:
            existing.session_id = session_id
        if run_id is not None:
            existing.run_id = run_id
        row = existing
    else:
        row = AiKnowledge(
            organization_id=organization_id,
            kind=kind_n,
            key=key_n,
            value=value_n,
            notes=notes_n,
            source=source_n,
            session_id=session_id,
            run_id=run_id,
        )
        db.add(row)

    await db.flush()
    return row


async def list_knowledge(
    db: AsyncSession,
    *,
    organization_id: str = ORG,
    kind: str | None = None,
    limit: int = _PROMPT_LIMIT,
) -> list[AiKnowledge]:
    lim = max(1, min(int(limit or _PROMPT_LIMIT), 200))
    q = select(AiKnowledge).where(AiKnowledge.organization_id == organization_id)
    if kind:
        q = q.where(AiKnowledge.kind == normalize_kind(kind))
    q = q.order_by(AiKnowledge.updated_at.desc(), AiKnowledge.id.desc()).limit(lim)
    return list((await db.execute(q)).scalars().all())


async def search_knowledge(
    db: AsyncSession,
    *,
    query: str,
    kind: str | None = None,
    organization_id: str = ORG,
    limit: int = 20,
) -> list[AiKnowledge]:
    qtext = (query or "").strip()
    lim = max(1, min(int(limit or 20), 50))
    stmt = select(AiKnowledge).where(AiKnowledge.organization_id == organization_id)
    if kind:
        stmt = stmt.where(AiKnowledge.kind == normalize_kind(kind))
    if qtext:
        like = f"%{qtext}%"
        stmt = stmt.where(
            or_(
                AiKnowledge.key.ilike(like),
                AiKnowledge.value.ilike(like),
                AiKnowledge.notes.ilike(like),
            )
        )
    stmt = stmt.order_by(AiKnowledge.updated_at.desc()).limit(lim)
    return list((await db.execute(stmt)).scalars().all())


def format_knowledge_for_prompt(rows: list[AiKnowledge]) -> str:
    """Compact glossary block for the system prompt."""
    if not rows:
        return ""
    lines = [
        "Learned company knowledge (use these abbreviations / short names / aliases; "
        "call remember_fact when the user teaches a new one):"
    ]
    for r in rows:
        note = f" ({r.notes})" if (r.notes or "").strip() else ""
        lines.append(f"- [{r.kind}] {r.key} → {r.value}{note}")
    return "\n".join(lines)


def row_to_dict(row: AiKnowledge) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "key": row.key,
        "value": row.value,
        "notes": row.notes or "",
        "source": row.source,
        "session_id": row.session_id,
        "run_id": row.run_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def seed_builtin_knowledge(db: AsyncSession, *, organization_id: str = ORG) -> list[str]:
    """Ensure standing org rules exist in ai_knowledge (idempotent upsert)."""
    touched: list[str] = []
    for item in BUILTIN_KNOWLEDGE:
        await upsert_knowledge(
            db,
            kind=item["kind"],
            key=item["key"],
            value=item["value"],
            notes=item.get("notes") or "",
            source=item.get("source") or "manual",
            organization_id=organization_id,
        )
        touched.append(item["key"])
    return touched


async def auto_learn_from_message(
    db: AsyncSession,
    message: str,
    *,
    session_id: int | None = None,
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    """Extract + upsert teachings from a user message. Returns saved rows as dicts."""
    teachings = extract_teachings(message)
    saved: list[dict[str, Any]] = []
    for t in teachings:
        row = await upsert_knowledge(
            db,
            kind=t["kind"],
            key=t["key"],
            value=t["value"],
            notes="auto-extracted from chat",
            source="chat",
            session_id=session_id,
            run_id=run_id,
        )
        saved.append(row_to_dict(row))
    return saved
