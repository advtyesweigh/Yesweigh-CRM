"""Role helpers and campaigns-only access rules."""
from __future__ import annotations

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _extract_token, bearer_from_request
from app.config import get_settings
from app.models import User

CAMPAIGNS_ONLY_ROLES = frozenset({"campaigns_only", "media_designer"})


def norm_role(role: str) -> str:
    return (role or "").strip().lower().replace(" ", "_")


def is_campaigns_only(user: User) -> bool:
    return norm_role(user.role) in CAMPAIGNS_ONLY_ROLES


def campaigns_only_path_allowed(path: str) -> bool:
    p = (path or "").split("?", 1)[0].rstrip("/") or "/"
    if p == "/api/health":
        return True
    if p.startswith("/api/auth/"):
        return True
    if p == "/api/me":
        return True
    if p.startswith("/api/campaigns"):
        return True
    if p.startswith("/api/whatsapp/media/"):
        return True
    # Campaigns-only roles do not get Call List / call-log APIs.
    return False


def require_campaigns_only_access(user: User, path: str) -> None:
    if is_campaigns_only(user) and not campaigns_only_path_allowed(path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access limited to Campaigns",
        )


async def optional_user_from_request(request: Request, db: AsyncSession) -> User | None:
    raw = _extract_token(request, bearer_from_request(request))
    if not raw:
        return None
    try:
        payload = jwt.decode(raw, get_settings().secret_key, algorithms=["HS256"])
        email = payload.get("sub")
        if not email:
            return None
    except JWTError:
        return None

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user or not user.is_active:
        return None

    token_sv = payload.get("sv")
    token_jti = payload.get("jti")
    client = str(payload.get("client") or "web").strip().lower()
    if client == "mobile":
        mobile = (user.mobile_session_id or "").strip()
        if not mobile or not token_jti or str(token_jti) != mobile:
            return None
        return user

    current_sv = int(user.session_version or 0)
    if token_sv is None:
        if current_sv != 0:
            return None
        return user
    try:
        if int(token_sv) != current_sv:
            return None
    except (TypeError, ValueError):
        return None

    active = (user.active_session_id or "").strip()
    if active and token_jti and str(token_jti) != active:
        return None
    return user
