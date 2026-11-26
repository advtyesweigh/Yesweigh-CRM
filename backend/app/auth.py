from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

SESSION_COOKIE = "crm_session"
CLIENT_MOBILE = "mobile"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def create_access_token(
    *,
    subject: str,
    session_version: int,
    jti: str,
    client: str = "web",
) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": subject,
        "exp": expire,
        "sv": int(session_version),
        "jti": jti,
        "client": client,
    }
    return jwt.encode(
        payload,
        settings.secret_key,
        algorithm="HS256",
    )


def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    max_age = max(60, int(settings.access_token_expire_minutes) * 60)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def new_session_id() -> str:
    return str(uuid4())


def bearer_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def _extract_token(request: Request, bearer: str | None) -> str | None:
    if bearer:
        return bearer
    cookie = request.cookies.get(SESSION_COOKIE)
    return cookie or None


async def issue_user_session(user: User, db: AsyncSession) -> str:
    """Create the sole active web session for this user (revokes all prior tokens)."""
    user.session_version = int(user.session_version or 0) + 1
    user.active_session_id = new_session_id()
    await db.commit()
    await db.refresh(user)
    return create_access_token(
        subject=user.email,
        session_version=user.session_version,
        jti=user.active_session_id,
        client="web",
    )


async def issue_mobile_session(user: User, db: AsyncSession) -> str:
    """Android YES CRM session — does not revoke the web CRM session."""
    user.mobile_session_id = new_session_id()
    await db.commit()
    await db.refresh(user)
    return create_access_token(
        subject=user.email,
        session_version=int(user.session_version or 0),
        jti=user.mobile_session_id,
        client=CLIENT_MOBILE,
    )


async def get_current_user(
    request: Request,
    token: Annotated[str | None, Depends(oauth2)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    session_revoked = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Session ended — signed in elsewhere",
        headers={"WWW-Authenticate": "Bearer"},
    )
    raw = _extract_token(request, token)
    if not raw:
        raise credentials_exception
    try:
        payload = jwt.decode(raw, get_settings().secret_key, algorithms=["HS256"])
        email = payload.get("sub")
        if not email:
            raise credentials_exception
    except JWTError as exc:
        raise credentials_exception from exc

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user or not user.is_active:
        raise credentials_exception

    token_sv = payload.get("sv")
    token_jti = payload.get("jti")
    client = str(payload.get("client") or "web").strip().lower()

    # Android YES CRM: validate against mobile_session_id only (web login stays valid).
    if client == CLIENT_MOBILE:
        mobile = (user.mobile_session_id or "").strip()
        if not mobile or not token_jti or str(token_jti) != mobile:
            raise session_revoked
        return user

    current_sv = int(user.session_version or 0)

    # Legacy JWTs (pre single-session) have no sv/jti — valid only until the user logs in again.
    if token_sv is None:
        if current_sv != 0:
            raise session_revoked
        return user

    try:
        if int(token_sv) != current_sv:
            raise session_revoked
    except (TypeError, ValueError) as exc:
        raise credentials_exception from exc

    active = (user.active_session_id or "").strip()
    if active and token_jti and str(token_jti) != active:
        raise session_revoked

    return user
