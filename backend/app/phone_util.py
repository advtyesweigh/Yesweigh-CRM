"""Normalize Indian mobile numbers to 10 digits."""

from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"\D+")


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
