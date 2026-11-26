"""One Intelligence row per WhatsApp conversation / phone / jid."""
from __future__ import annotations

import re
from typing import Any, Callable

from app.ai.whatsapp_send_guards import looks_like_container_strike_notice, phone_key

_BLAST_RE = re.compile(
    r"(?i)("
    r"dear\s+customer|container\s+strike|resume\s+dispatch|"
    r"പ്രിയ\s*ഉപഭോക്താവ്|കണ്ടൈനർ\s*സ്ട്രൈക്ക്|കണ്ടെയ്നർ\s*സമരം"
    r")"
)


def is_blast_summary(text: str | None) -> bool:
    blob = (text or "").strip()
    if not blob:
        return False
    if looks_like_container_strike_notice(blob):
        return True
    return bool(_BLAST_RE.search(blob))


def _row_phone(row: dict[str, Any]) -> str:
    return phone_key(str(row.get("phone") or row.get("customer_phone") or ""))


def _row_jid(row: dict[str, Any]) -> str:
    return (str(row.get("customer_jid") or row.get("jid") or "")).strip().lower()


def _row_conv_key(row: dict[str, Any]) -> str:
    for key in ("conversation_id", "id", "subject_id", "public_id"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return f"conv:{str(val).strip()}"
    jid = _row_jid(row)
    if jid:
        return f"jid:{jid}"
    phone = _row_phone(row)
    if phone:
        return f"phone:{phone}"
    return ""


def _summary_of(row: dict[str, Any]) -> str:
    return str(row.get("summary") or row.get("promise_text") or row.get("subtitle") or "")


def summary_quality(text: str | None) -> int:
    """Higher = prefer this summary when collapsing duplicates."""
    blob = (text or "").strip()
    if not blob:
        return -50
    if is_blast_summary(blob):
        return -100
    score = 10
    low = blob.lower()
    if re.search(r"(?i)\bov\b|stamping|ഓവി|ഒവി", blob):
        score += 40
    if re.search(r"(?i)\d+\s*min|will\s+(?:send|call|check)|അയച്ചു?\s*തര", blob):
        score += 25
    if low.startswith("voice message") or "voice message" in low:
        score += 5
    # Prefer concrete short promises over long blast-ish copy.
    if len(blob) > 160:
        score -= 15
    return score


def pick_better_row(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    qa = summary_quality(_summary_of(a))
    qb = summary_quality(_summary_of(b))
    if qa != qb:
        return a if qa > qb else b
    age_a = int(a.get("age_seconds") or 0)
    age_b = int(b.get("age_seconds") or 0)
    if age_a != age_b:
        return a if age_a >= age_b else b
    return a


def dedupe_intel_rows(
    rows: list[dict[str, Any]],
    *,
    drop_blast: bool = False,
    key_fn: Callable[[dict[str, Any]], str] | None = None,
) -> list[dict[str, Any]]:
    """Collapse clones so each conversation/phone/jid appears once (order preserved)."""
    key_of = key_fn or _row_conv_key
    chosen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    phone_to_key: dict[str, str] = {}
    jid_to_key: dict[str, str] = {}

    for row in rows:
        if drop_blast and is_blast_summary(_summary_of(row)):
            continue
        key = key_of(row)
        if not key:
            # Keep unique unkeyed rows as-is.
            synth = f"anon:{len(order)}"
            chosen[synth] = row
            order.append(synth)
            continue

        phone = _row_phone(row)
        jid = _row_jid(row)
        # Merge same phone/jid even when conversation public ids differ.
        if phone and phone in phone_to_key:
            key = phone_to_key[phone]
        elif jid and jid in jid_to_key:
            key = jid_to_key[jid]

        if key not in chosen:
            chosen[key] = row
            order.append(key)
            if phone:
                phone_to_key[phone] = key
            if jid:
                jid_to_key[jid] = key
            continue

        chosen[key] = pick_better_row(chosen[key], row)
        if phone:
            phone_to_key[phone] = key
        if jid:
            jid_to_key[jid] = key

    return [chosen[k] for k in order]
