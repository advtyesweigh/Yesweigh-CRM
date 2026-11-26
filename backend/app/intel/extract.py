"""Rule extractors for commitments and secondary intents.

Gemini is not called. Nothing here writes ``awaiting_staff_reply`` or
``service_category``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Any

from app.whatsapp_chat_types import is_broadcast_or_status_jid, is_group_jid, is_newsletter_jid

# Observe-only. A unit test fails if this text starts giving procedures.
SERVICE_OBSERVE_POLICY = (
    "Observe only. Calibration procedure, firmware steps, and legal-metrology "
    "advice are forbidden. Tag the thread and assign a service engineer. "
    "Do not explain how to calibrate, flash firmware, or stamp a scale."
)

SECONDARY_INTENT_ALLOWLIST = (
    "price",
    "quotation",
    "order",
    "payment",
    "dispatch_chatter",
    "repair",
    "install",
    "warranty",
    "cloud_renew",
    "complaint",
    "dealer_enquiry",
    "stamping",
)

_INTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("price", re.compile(r"(?i)\b(price|pricing|rate|cost|how\s*much|dealer\s*price)\b")),
    ("quotation", re.compile(r"(?i)\b(quote|quotation)\b")),
    ("order", re.compile(r"(?i)\b(order|purchase|buy)\b")),
    ("payment", re.compile(r"(?i)\b(payment|paid|balance|invoice|pending\s*payment)\b")),
    ("dispatch_chatter", re.compile(r"(?i)\b(dispatch|courier|shipped|shipping|delivery)\b")),
    ("repair", re.compile(r"(?i)\b(repair|repaired|not\s*working|fault|broken|defect)\b")),
    ("install", re.compile(r"(?i)\b(install|installation)\b")),
    ("warranty", re.compile(r"(?i)\b(warranty|amc)\b")),
    ("cloud_renew", re.compile(r"(?i)\b(renew|subscription|weighing\s*sync|cloud\s*recharge)\b")),
    ("complaint", re.compile(r"(?i)\b(complaint|unhappy|worst|refund|cheat|frustrated)\b")),
    ("dealer_enquiry", re.compile(r"(?i)\b(dealer\s*enquir|become\s+a?\s*dealer|wholesale\s*price)\b")),
    (
        "stamping",
        re.compile(
            r"(?i)\b(ov\b|verification\s*certificate|kg\s*ov|camp\s*stamp|stamping\b|inspector|metrology)\b"
            r"|ഓവി|സ്റ്റാമ്പ്"
        ),
    ),
)

# Short labels only. Never a procedure sentence.
_SERVICE_TAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("repair", re.compile(r"(?i)\b(?:repair|not\s*working|fault|broken|defect)\b")),
    ("install", re.compile(r"(?i)\binstall")),
    ("warranty", re.compile(r"(?i)\b(?:warranty|\bamc\b)")),
    ("visit", re.compile(r"(?i)\b(?:technician|engineer\s*visit|onsite|service\s*visit)\b")),
    ("calibration", re.compile(r"(?i)\bcalibrat")),
    ("spare", re.compile(r"(?i)\b(?:spare\s*part|dust\s*cover|motherboard)\b")),
)

_PROMISE = re.compile(
    r"(?i)\b("
    r"will\s+(?:send|share|call|dispatch|deliver|revert|confirm|arrange|visit|pay|check|look)|"
    r"i'?ll\s+(?:send|share|call|dispatch|deliver|check|look)|"
    r"we\s+will\b|we'?ll\b|"
    r"promise\s+to\b|"
    r"send\s+(?:the\s+)?(?:quote|quotation)|"
    r"dispatch\s+tomorrow|"
    r"look\s+into|checking|"
    r"\d+\s*min(?:ute)?s?|"
    r"2\s*min\b"
    r")"
    # Malayalam staff promise — not bare "നോക്ക" voice fillers.
    r"|അയച്ചു?\s*തരാ|കണ്ടു\s*നോക്കാം|നോക്കി\s*തര|വിളിക്കാം|അയക്കാം|"
    r"പരിശോധിച്ച്\s*തര|അഞ്ചുപത്ത്\s*മിനിറ്റ്|മിനിറ്റ്.{0,24}അയച്ചു?\s*തര"
)

_BLAST_COMMITMENT = re.compile(
    r"(?i)("
    r"dear\s+customer|container\s+strike|resume\s+dispatch|"
    r"പ്രിയ\s*ഉപഭോക്താവ്|കണ്ടൈനർ\s*സ്ട്രൈക്ക്|കണ്ടെയ്നർ\s*സമരം"
    r")"
)
_WEEKDAY = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_WEEKDAY_RE = re.compile(
    r"(?i)\b(?:by\s+|on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_ISO_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DMY_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")


def conversation_is_intel_subject(jid: str | None) -> bool:
    """DMs only. Groups, newsletters, and status/broadcast are never subjects."""
    if is_group_jid(jid) or is_newsletter_jid(jid) or is_broadcast_or_status_jid(jid):
        return False
    return True


def enqueue_decision(*, enabled: bool, jid: str | None) -> bool:
    """Webhook gate: flag off or non-DM → do not insert an ai_jobs row."""
    if not enabled:
        return False
    return conversation_is_intel_subject(jid)


def idempotency_key(job_type: str, subject_type: str, subject_id: str, fingerprint: str) -> str:
    return f"{job_type}:{subject_type}:{subject_id}:{fingerprint}"


def should_insert_job(*, existing_key: str | None, new_key: str) -> bool:
    if existing_key and existing_key == new_key:
        return False
    return bool(new_key)


def service_keyword_tags(text: str) -> list[str]:
    """Labels only. The strings are short tokens, not instructions."""
    blob = text or ""
    out: list[str] = []
    for tag, pattern in _SERVICE_TAGS:
        if pattern.search(blob) and tag not in out:
            out.append(tag)
    return out


def apply_secondary_intents(conversation: Any, intents: list[str]) -> tuple[str, list[str]]:
    """Copy the inbox chip. Never write ``service_category``."""
    primary = str(getattr(conversation, "service_category", "") or "")
    cleaned: list[str] = []
    for raw in intents:
        key = (raw or "").strip().lower()
        if key in SECONDARY_INTENT_ALLOWLIST and key not in cleaned:
            cleaned.append(key)
    return primary, cleaned


def detect_secondary_intents(*parts: str) -> list[str]:
    blob = "\n".join(p for p in parts if p)
    found: list[str] = []
    for key, pattern in _INTENT_RULES:
        if pattern.search(blob) and key not in found:
            found.append(key)
    return found


def _next_weekday(today: date, weekday: int) -> date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def parse_due_on(text: str, today: date) -> date | None:
    blob = text or ""
    low = blob.lower()
    if re.search(r"\btomorrow\b", low):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", low):
        return today
    iso = _ISO_RE.search(blob)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    dmy = _DMY_RE.search(blob)
    if dmy:
        day_n, month_n, year_n = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        if year_n < 100:
            year_n += 2000
        try:
            return date(year_n, month_n, day_n)
        except ValueError:
            return None
    wd = _WEEKDAY_RE.search(blob)
    if wd:
        return _next_weekday(today, _WEEKDAY[wd.group(1).lower()])
    return None


def commitment_fingerprint(conversation_id: int, message_id: int | None, promise: str) -> str:
    raw = f"{conversation_id}|{message_id or 0}|{(promise or '').strip().lower()[:180]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def next_commitment_status(status: str, due_on: date | None, today: date) -> str:
    """Missed only when a due date is already past. No due date stays open.

    This returns a commitment status string. It does not receive or change
    ``awaiting_staff_reply``.
    """
    current = (status or "open").strip().lower()
    if current != "open":
        return current
    if due_on is None:
        return "open"
    if due_on < today:
        return "missed"
    return "open"


def extract_commitment_from_message(
    *,
    body: str,
    transcript: str = "",
    direction: str,
    today: date,
    message_id: int | None,
    conversation_id: int,
) -> dict[str, Any] | None:
    """One rule hit per message, or None. Stored as open. Extractor is ``rule``.

    The Commitments tile lists staff promises: the agent committed to give or do something.
    """
    text = " ".join(p.strip() for p in (body or "", transcript or "") if p and p.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text or not _PROMISE.search(text):
        return None
    # Ops blasts ("Dear Customer… we will resume…") are not staff promises.
    if _BLAST_COMMITMENT.search(text):
        return None
    try:
        from app.ai.whatsapp_send_guards import looks_like_container_strike_notice

        if looks_like_container_strike_notice(text):
            return None
    except Exception:  # noqa: BLE001
        pass
    quote = text[:240]
    due = parse_due_on(text, today)
    side = "staff" if (direction or "").lower() in {"out", "outbound"} else "customer"
    if (direction or "").lower() not in {"in", "inbound", "out", "outbound"}:
        side = "unclear"
    if side == "unclear":
        confidence = "low"
    elif due is None:
        confidence = "medium"
    else:
        confidence = "high"
    return {
        "conversation_id": conversation_id,
        "promise_text": quote,
        "due_on": due,
        "owner_side": side,
        "status": "open",
        "evidence_message_id": message_id,
        "extractor": "rule",
        "confidence": confidence,
        "fingerprint": commitment_fingerprint(conversation_id, message_id, quote),
        "high_lane": confidence != "low",
    }
