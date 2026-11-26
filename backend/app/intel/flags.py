"""Feature flag and role gate for the Observe board.

Default is off. Routes that read findings return 404 until ``ai_settings.intel_enabled``
is true. Inbox webhooks do not insert jobs while the flag is off.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiSettings
from app.permissions import norm_role

ORG = "yesweigh"

# Full board: management and sales managers.
_FULL_ROLES = frozenset(
    {
        "admin",
        "super_admin",
        "management",
        "sales_manager",
        "owner",
        "director",
    }
)
_STAFF_REPLY_ROLES = frozenset({"admin", "super_admin", "management"})
_ADMIN_ROLES = frozenset({"admin", "super_admin"})


def intel_enabled_value(row: object | None) -> bool:
    """Safe default: missing row or missing column means off."""
    if row is None:
        return False
    return bool(getattr(row, "intel_enabled", False))


async def intel_is_enabled(db: AsyncSession) -> bool:
    row = (
        await db.execute(select(AiSettings).where(AiSettings.organization_id == ORG))
    ).scalar_one_or_none()
    return intel_enabled_value(row)


def intel_access(user) -> str | None:
    """``full`` | ``service`` | None.

    Service managers see service threads only. Campaigns-only roles never reach
    this helper (middleware returns 403 first).
    """
    role = norm_role(getattr(user, "role", "") or "")
    if not role:
        return None
    if "service" in role and "manager" in role:
        return "service"
    if role in _FULL_ROLES or role.endswith("_admin") or "admin" in role:
        return "full"
    if "manager" in role or "management" in role:
        return "full"
    return None


def can_view_staff_reply(user) -> bool:
    return norm_role(getattr(user, "role", "") or "") in _STAFF_REPLY_ROLES


def can_rebuild(user) -> bool:
    role = norm_role(getattr(user, "role", "") or "")
    return role in _ADMIN_ROLES or role == "management"
