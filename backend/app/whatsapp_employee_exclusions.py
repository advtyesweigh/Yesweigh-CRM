"""ADMS enrollment codes not eligible for a personal WhatsApp instance.

Active office staff who should not get a YesWeigh WhatsApp line.
Left/retired codes stay in adms_employee_exclusions — do not conflate them here.

New hires (enrollment 24+) remain eligible unless added to this set (or env).
"""
from __future__ import annotations

import os
from functools import lru_cache

# Ashima (23), Jeemol (22), Fazal (20)
WHATSAPP_EXCLUDED_EMPLOYEE_CODES: frozenset[str] = frozenset({"20", "22", "23"})


def _env_extra_codes() -> frozenset[str]:
    raw = (os.environ.get("WHATSAPP_EXCLUDED_EMPLOYEE_CODES") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.replace(";", ",").split(",") if p.strip())


@lru_cache
def whatsapp_excluded_employee_codes() -> frozenset[str]:
    return frozenset({*WHATSAPP_EXCLUDED_EMPLOYEE_CODES, *_env_extra_codes()})


def normalize_code(code: str | None) -> str:
    c = (code or "").strip()
    if c.isdigit():
        return str(int(c))
    return c


def is_whatsapp_excluded_employee_code(code: str | None) -> bool:
    c = normalize_code(code)
    if not c:
        return False
    return c in whatsapp_excluded_employee_codes()
