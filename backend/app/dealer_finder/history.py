"""Smart Search History — cooldown before Places calls; reuse prior results."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_finder.constants import (
    ORG_DEFAULT,
    STATUS_ALREADY_SEARCHED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
)
from app.dealer_finder.normalize import identity_hash, search_identity
from app.models import DealerSearchLog, DealerSearchPlace


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def find_recent_log(
    db: AsyncSession,
    *,
    state: str,
    district: str,
    city: str,
    category: str,
    cooldown_days: int = 30,
    organization_id: str = ORG_DEFAULT,
    max_results: int = 60,
    page_size: int = 20,
) -> DealerSearchLog | None:
    ident = search_identity(
        organization_id=organization_id,
        state=state,
        district=district,
        city=city,
        category=category,
        max_results=max_results,
        page_size=page_size,
    )
    h = identity_hash(ident)
    row = (
        await db.execute(select(DealerSearchLog).where(DealerSearchLog.identity_hash == h))
    ).scalar_one_or_none()
    if not row:
        return None
    if row.status == STATUS_RUNNING:
        return row
    if not row.searched_at:
        return None
    age = _now() - row.searched_at
    if age <= timedelta(days=max(1, int(cooldown_days))):
        return row
    return None


async def get_or_claim_log(
    db: AsyncSession,
    *,
    state: str,
    district: str,
    city: str,
    category: str,
    text_query: str,
    job_id: int | None,
    user_id: int | None,
    force_refresh: bool = False,
    cooldown_days: int = 30,
    organization_id: str = ORG_DEFAULT,
    max_results: int = 60,
    page_size: int = 20,
) -> tuple[DealerSearchLog, str]:
    """Return (log, action) where action is reuse|resume|fresh.

    reuse → within cooldown, skip Places
    resume → Running with partial pages
    fresh → call Places
    """
    ident = search_identity(
        organization_id=organization_id,
        state=state,
        district=district,
        city=city,
        category=category,
        max_results=max_results,
        page_size=page_size,
    )
    h = identity_hash(ident)
    row = (
        await db.execute(select(DealerSearchLog).where(DealerSearchLog.identity_hash == h))
    ).scalar_one_or_none()

    if row and row.status == STATUS_RUNNING:
        return row, "resume"

    if row and not force_refresh and row.searched_at:
        age = _now() - row.searched_at
        if age <= timedelta(days=max(1, int(cooldown_days))):
            if row.status not in {STATUS_ALREADY_SEARCHED, STATUS_COMPLETED}:
                row.status = STATUS_ALREADY_SEARCHED
            return row, "reuse"

    if not row:
        row = DealerSearchLog(
            identity_hash=h,
            organization_id=organization_id,
            country=str(ident["country"]),
            state=str(ident["state"]),
            district=str(ident["district"]),
            city=str(ident["city"]),
            category=str(ident["category"]),
            language=str(ident["language"]),
            plan_version=str(ident["plan_version"]),
            max_results=int(ident["max_results"]),
            page_size=int(ident["page_size"]),
            text_query=text_query[:500],
            status=STATUS_RUNNING,
            job_id=job_id,
            searched_by=user_id,
            force_refresh=force_refresh,
            next_page_token="",
            pages_done=0,
        )
        db.add(row)
        await db.flush()
        return row, "fresh"

    # Refresh / expired — reclaim
    row.status = STATUS_RUNNING
    row.job_id = job_id
    row.text_query = text_query[:500]
    row.force_refresh = force_refresh
    row.next_page_token = ""
    row.pages_done = 0
    row.searched_by = user_id
    await db.flush()
    return row, "fresh"


async def places_for_log(db: AsyncSession, log_id: int) -> list[DealerSearchPlace]:
    rows = (
        await db.execute(
            select(DealerSearchPlace).where(DealerSearchPlace.search_log_id == log_id)
        )
    ).scalars().all()
    return list(rows)


def log_public(row: DealerSearchLog, *, place_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "identity_hash": row.identity_hash,
        "state": row.state,
        "district": row.district,
        "city": row.city,
        "category": row.category,
        "text_query": row.text_query,
        "status": row.status,
        "results_count": row.results_count,
        "with_phone_count": row.with_phone_count,
        "with_website_count": row.with_website_count,
        "pages_fetched": row.pages_fetched,
        "api_requests": row.api_requests,
        "searched_at": row.searched_at.isoformat() if row.searched_at else None,
        "place_ids": place_ids or [],
        "previously_searched": row.status
        in {STATUS_ALREADY_SEARCHED, STATUS_COMPLETED},
    }
