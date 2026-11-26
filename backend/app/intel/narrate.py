"""Narration guards. Phase Observe does not call Gemini.

``numbers_guard`` drops wording that introduces a numeral absent from the SQL
payload. It is unused by the board until Recommend (Phase B).
"""
from __future__ import annotations

import re

_NUM = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    return set(_NUM.findall(text or ""))


def numbers_guard(narration: str | None, source_payload: str | None) -> str | None:
    """Return narration only when every numeral already appears in the source text."""
    text = (narration or "").strip()
    if not text:
        return None
    extra = numbers_in(text) - numbers_in(source_payload or "")
    if extra:
        return None
    return text
