"""Past/left ADMS employee codes — never re-import or sync into CRM.

Current roster uses enrollment codes 3,6,14,18,19,20,22,23.
New hires start at 24+. Do not reuse these retired codes.
"""
from __future__ import annotations

import os
from functools import lru_cache

# Hard-retired enrollment PINs (left staff + odd 21/"55" alias).
EXCLUDED_EMPLOYEE_CODES: frozenset[str] = frozenset(
    {
        "1",
        "2",
        "4",
        "5",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
        "13",
        "15",
        "16",
        "17",
        "21",
        "55",
    }
)


def _env_extra_codes() -> frozenset[str]:
    raw = (os.environ.get("ADMS_EXCLUDED_EMPLOYEE_CODES") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.replace(";", ",").split(",") if p.strip())


@lru_cache
def excluded_employee_codes() -> frozenset[str]:
    return frozenset({*EXCLUDED_EMPLOYEE_CODES, *_env_extra_codes()})


def is_excluded_employee_code(code: str | None) -> bool:
    c = (code or "").strip()
    if not c:
        return False
    excluded = excluded_employee_codes()
    if c in excluded:
        return True
    # Numeric forms: "01" vs "1"
    if c.isdigit() and str(int(c)) in excluded:
        return True
    return False
