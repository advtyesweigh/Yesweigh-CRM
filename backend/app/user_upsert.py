"""One-off CLI to create or update a CRM login (run inside API container)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select

from app.auth import hash_password
from app.db import SessionLocal
from app.models import User


async def upsert_user(*, email: str, name: str, role: str, password: str) -> User:
    email_norm = email.strip().lower()
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email_norm))).scalar_one_or_none()
        if user:
            user.name = name.strip() or user.name
            user.role = role.strip() or user.role
            user.password_hash = hash_password(password)
            user.is_active = True
            action = "updated"
        else:
            user = User(
                email=email_norm,
                name=name.strip() or email_norm.split("@")[0],
                password_hash=hash_password(password),
                role=role.strip() or "employee",
                is_active=True,
            )
            db.add(user)
            action = "created"
        await db.commit()
        await db.refresh(user)
        print(f"{action} user id={user.id} email={user.email} role={user.role}")
        return user


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Create or update CRM user")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--role", default="media_designer")
    p.add_argument(
        "--password-env",
        default="CRM_USER_PASSWORD",
        help="Env var holding plaintext password (never logged)",
    )
    args = p.parse_args(argv)
    password = os.environ.get(args.password_env, "").strip()
    if not password:
        print(f"Missing password in env {args.password_env}", file=sys.stderr)
        return 1
    asyncio.run(
        upsert_user(
            email=args.email,
            name=args.name,
            role=args.role,
            password=password,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
