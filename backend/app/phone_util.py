"""Normalize Indian mobile numbers to 10 digits."""

from __future__ import annotations

import re
from typing import Literal

_NON_DIGIT = re.compile(r"\D+")

PhoneKind = Literal["empty", "mobile", "landline", "invalid"]


def normalize_in_mobile(value: str | None) -> str:
    """
    Strip non-digits, drop leading country/trunk prefixes, return 10 digits or "".

    - Strip non-digits
    - If starts with 91 and length > 10, drop leading 91
    - If starts with 0 and length 11, drop leading 0
    - Keep final 10 digits when possible; else ""
    """
    digits = _NON_DIGIT.sub("", value or "")
    if not digits:
        return ""
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) >= 10:
        return digits[-10:]
    return ""


def classify_indian_phone(value: str | None) -> PhoneKind:
    """Classify an Indian phone string as mobile, landline, empty, or invalid.

  Mobile: 10-digit national starting 6–9, optional ``91`` prefix or trunk ``0``.
  Landline: STD / shorter subscriber (leading ``0`` + non-6–9, or <10 digits ≥6),
    or 10-digit national not starting 6–9 (e.g. ``0484…`` stored without trunk).
  """
    digits = _NON_DIGIT.sub("", value or "")
    if not digits:
        return "empty"
    national = digits
    if national.startswith("91") and len(national) > 10:
        national = national[2:]
    if len(national) == 11 and national[0] == "0":
        if national[1] in "6789":
            return "mobile"
        return "landline"
    if len(national) == 10:
        if national[0] in "6789":
            return "mobile"
        return "landline"
    if len(national) < 10:
        if len(national) >= 6:
            return "landline"
        return "invalid"
    # Longer national forms (STD + local without normalizing to mobile)
    return "landline"


def is_indian_mobile(value: str | None) -> bool:
    """True when the value is a usable Indian mobile (WhatsApp-eligible pattern)."""
    return classify_indian_phone(value) == "mobile"


def to_evolution_number(value: str | None) -> str:
    """
    Digits suitable for Evolution GO send/check with formatJid.

    Indian 10-digit mobiles (starting 6–9) become ``91XXXXXXXXXX``. Without the
    country code Evolution treats the leading digits as a country code
    (e.g. ``7592052528`` → ``+7592052528``) and fails with "not registered".
    Pass through ``@lid`` / full JIDs unchanged.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        # Explicit JID (…@lid / …@s.whatsapp.net / …@g.us) — leave as-is
        return raw
    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        return ""
    if len(digits) == 10 and digits[0] in "6789":
        return f"91{digits}"
    if digits.startswith("91") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 11 and digits[1] in "6789":
        return f"91{digits[1:]}"
    return digits


def whatsapp_pn_jid(value: str | None) -> str:
    """Build ``91…@s.whatsapp.net`` (or pass-through non-IN) for new conversations."""
    number = to_evolution_number(value)
    if not number or "@" in number:
        return number
    return f"{number}@s.whatsapp.net"


def repair_stored_in_mobile(value: str | None) -> tuple[str, bool]:
    """Upgrade bare Indian mobiles in stored CRM fields to ``91XXXXXXXXXX``.

    Returns ``(new_value, changed)``.

    Repairs only:
    - 10-digit mobiles starting 6–9
    - 11-digit trunk ``0`` + 10-digit mobile
    - ``+91…`` / spaced forms that normalize to a valid IN mobile (canonical digits)

    Does **not** double-prefix numbers already ``91XXXXXXXXXX``, leave non-IN /
    short / LID / JID / empty values alone (except stripping ``+91`` formatting
    into canonical ``91…`` digits when unambiguous).
    """
    raw = (value or "").strip()
    if not raw or "@" in raw:
        return raw, False
    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        return raw, False
    # Already canonical 91 + 10-digit IN mobile
    if digits.startswith("91") and len(digits) == 12 and digits[2] in "6789":
        if digits == raw:
            return raw, False
        return digits, True
    # Bare 10-digit IN mobile
    if len(digits) == 10 and digits[0] in "6789":
        return f"91{digits}", True
    # Trunk 0 + 10-digit
    if len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        return f"91{digits[1:]}", True
    # Longer but ends with IN mobile after dropping leading 91 / 0 noise
    evo = to_evolution_number(raw)
    if evo and evo != raw and len(evo) == 12 and evo.startswith("91") and evo[2] in "6789":
        return evo, True
    return raw, False


def store_crm_phone(value: str | None) -> str:
    """CRM storage form: ``91XXXXXXXXXX`` for Indian mobiles when unambiguous."""
    stored, _ = repair_stored_in_mobile(value)
    return stored


def repair_stored_whatsapp_jid(jid: str | None, phone: str | None = None) -> tuple[str, bool]:
    """Repair bare ``10digit@s.whatsapp.net`` JIDs to ``91…@s.whatsapp.net``.

    Prefer repairing from ``phone`` when it yields a valid Evolution number.
    Leaves ``@lid`` / ``@g.us`` / already-correct JIDs unchanged.
    """
    raw = (jid or "").strip()
    if phone:
        evo = to_evolution_number(phone)
        if evo and "@" not in evo and len(evo) == 12 and evo.startswith("91"):
            fixed = f"{evo}@s.whatsapp.net"
            if fixed != raw:
                return fixed, True
            return raw, False
    if not raw or "@" not in raw:
        return raw, False
    user, _, host = raw.partition("@")
    if host != "s.whatsapp.net":
        return raw, False
    repaired, changed = repair_stored_in_mobile(user)
    if not changed:
        return raw, False
    return f"{repaired}@s.whatsapp.net", True