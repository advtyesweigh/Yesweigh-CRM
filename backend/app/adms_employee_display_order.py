"""Fixed CRM display order for active biometric staff (enrollment codes).

Keep in sync with the live office roster. Unknown / new codes sort after these.
Another process may retire other codes via adms_employee_exclusions — do not
fight those deletes here; only rank the active eight.
"""
from __future__ import annotations

# Lower rank = higher on Attendance Records / Employees lists.
STAFF_DISPLAY_ORDER: dict[str, int] = {
    "3": 1,  # Biju
    "18": 2,  # Visakh
    "14": 3,  # Shibin
    "19": 4,  # Safna
    "6": 5,  # Shalima
    "20": 6,  # Fazal
    "22": 7,  # Jeemol
    "23": 8,  # Ashima
}

UNKNOWN_RANK = 9_000


def normalize_employee_code(code: str | None) -> str:
    c = (code or "").strip()
    if c.isdigit():
        return str(int(c))
    return c


def staff_display_rank(code: str | None) -> int:
    """Sort key for enrollment / CRM employee codes. Unknown → after roster."""
    c = normalize_employee_code(code)
    if not c:
        return UNKNOWN_RANK + 1
    return STAFF_DISPLAY_ORDER.get(c, UNKNOWN_RANK)
