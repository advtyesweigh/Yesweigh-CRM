"""Google Places (New) two-stage search for Dealer Finder — reuses pagination rules."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_finder.constants import FIELD_MASK_STAGE1, FIELD_MASK_STAGE2, ORG_DEFAULT
from app.dealer_finder.normalize import maps_url_for_place, validate_website_url, whatsapp_display
from app.enrich_leads_places import PLACES_SEARCH_URL, extract_city, format_phone
from app.google_places_search import places_api_key
from app.dealer_finder.places_pricing import estimated_billable_usd
from app.models import DealerPlacesUsage


def _extract_state(components: list[dict[str, Any]], fallback: str) -> str:
    for c in components or []:
        types = c.get("types") or []
        if "administrative_area_level_1" in types:
            text = str(c.get("longText") or c.get("shortText") or "").strip()
            if text:
                return text[:80]
    return (fallback or "").strip()


def _extract_district(components: list[dict[str, Any]], fallback: str) -> str:
    for c in components or []:
        types = c.get("types") or []
        if "administrative_area_level_2" in types:
            text = str(c.get("longText") or c.get("shortText") or "").strip()
            if text:
                return text[:80]
    return (fallback or "").strip()


def place_to_finder_row(
    place: dict[str, Any],
    *,
    state_fallback: str = "",
    district_fallback: str = "",
    category: str = "",
) -> dict[str, Any]:
    display = ((place.get("displayName") or {}).get("text")) or ""
    addr = (place.get("formattedAddress") or "").strip()
    comps = place.get("addressComponents") or []
    city = extract_city(comps, district_fallback or state_fallback)
    state = _extract_state(comps, state_fallback)
    district = _extract_district(comps, district_fallback)
    phone = format_phone(
        place.get("nationalPhoneNumber") or "",
        place.get("internationalPhoneNumber") or "",
    )
    website = validate_website_url(str(place.get("websiteUri") or ""))
    place_id = str(place.get("id") or "").strip()
    return {
        "name": display.strip(),
        "company": display.strip(),
        "address": addr,
        "city": city,
        "district": district or district_fallback,
        "state": state or state_fallback,
        "phone": phone,
        "whatsapp": whatsapp_display(phone),
        "website": website,
        "place_id": place_id,
        "maps_url": maps_url_for_place(place_id),
        "category": category,
        "lead_source": "Google",
        "source": "google_places",
    }


async def search_places_staged(
    *,
    text_query: str,
    max_results: int = 60,
    page_size: int = 20,
    start_page_token: str = "",
    pages_already_done: int = 0,
    fetch_contacts: bool = True,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Stage-1 Text Search (cheap fields) + optional Stage-2 contact enrichment.

    Pagination: pageSize ≤20, ≤60 total, resumable via page token.
    """
    from app.google_places_search import _DEFAULT_MAX_PAGES, _DEFAULT_PAGE_SIZE, _GOOGLE_HARD_MAX

    key = (api_key or places_api_key()).strip()
    if not key:
        return {
            "ok": False,
            "error": "Google Places API key not configured",
            "places": [],
            "query": text_query,
            "pages": 0,
            "stage1_requests": 0,
            "stage2_requests": 0,
            "next_page_token": "",
        }

    max_results = max(1, min(_GOOGLE_HARD_MAX, int(max_results or 60)))
    page_size = max(1, min(_DEFAULT_PAGE_SIZE, int(page_size or 20)))
    pages_needed = min(_DEFAULT_MAX_PAGES, (max_results + page_size - 1) // page_size)
    pages_left = max(0, pages_needed - max(0, int(pages_already_done or 0)))

    collected: list[dict[str, Any]] = []
    page_token = (start_page_token or "").strip()
    pages = 0
    stage1 = 0
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            for _ in range(pages_left or pages_needed):
                body: dict[str, Any] = {
                    "textQuery": text_query,
                    "regionCode": "IN",
                    "pageSize": page_size,
                    "languageCode": "en",
                }
                if page_token:
                    body["pageToken"] = page_token
                resp = await client.post(
                    PLACES_SEARCH_URL,
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": key,
                        "X-Goog-FieldMask": FIELD_MASK_STAGE1,
                    },
                    json=body,
                )
                stage1 += 1
                pages += 1
                if resp.status_code >= 400:
                    return {
                        "ok": False,
                        "error": f"Google Places HTTP {resp.status_code}",
                        "places": collected,
                        "query": text_query,
                        "pages": pages,
                        "stage1_requests": stage1,
                        "stage2_requests": 0,
                        "next_page_token": page_token,
                        "http_status": resp.status_code,
                    }
                data = resp.json()
                batch = list(data.get("places") or [])
                collected.extend(batch)
                page_token = str(data.get("nextPageToken") or "").strip()
                if not page_token or len(collected) >= max_results:
                    break
    except httpx.TimeoutException:
        return {
            "ok": False,
            "error": "Google Places request timed out",
            "places": collected,
            "query": text_query,
            "pages": pages,
            "stage1_requests": stage1,
            "stage2_requests": 0,
            "next_page_token": page_token,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"Google Places error: {type(exc).__name__}",
            "places": collected,
            "query": text_query,
            "pages": pages,
            "stage1_requests": stage1,
            "stage2_requests": 0,
            "next_page_token": page_token,
        }

    places = collected[:max_results]
    stage2 = 0
    if fetch_contacts and places:
        enriched, stage2 = await _enrich_contacts(places, api_key=key)
        places = enriched

    return {
        "ok": True,
        "places": places,
        "query": text_query,
        "pages": pages + int(pages_already_done or 0),
        "stage1_requests": stage1,
        "stage2_requests": stage2,
        "next_page_token": page_token if len(collected) < max_results else "",
        "total_raw": len(places),
    }


async def _enrich_contacts(
    places: list[dict[str, Any]], *, api_key: str
) -> tuple[list[dict[str, Any]], int]:
    """Stage-2: Place Details for phone + websiteUri only (Enterprise cost control)."""
    out: list[dict[str, Any]] = []
    requests = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for p in places:
            pid = str(p.get("id") or "").strip()
            if not pid:
                out.append(p)
                continue
            # Already has contact fields from a prior enrich
            if p.get("nationalPhoneNumber") or p.get("websiteUri"):
                out.append(p)
                continue
            # Resource name is "places/ChIJ…" — Text Search returns that as id.
            resource = pid if pid.startswith("places/") else f"places/{pid}"
            try:
                resp = await client.get(
                    f"https://places.googleapis.com/v1/{resource}",
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": api_key,
                        "X-Goog-FieldMask": "id,nationalPhoneNumber,internationalPhoneNumber,websiteUri",
                    },
                )
                requests += 1
                if resp.status_code >= 400:
                    out.append(p)
                    continue
                detail = resp.json() or {}
                merged = dict(p)
                for k in (
                    "nationalPhoneNumber",
                    "internationalPhoneNumber",
                    "websiteUri",
                ):
                    if detail.get(k):
                        merged[k] = detail[k]
                out.append(merged)
            except Exception:  # noqa: BLE001
                out.append(p)
    return out, requests


async def record_usage(
    db: AsyncSession,
    *,
    organization_id: str = ORG_DEFAULT,
    stage1: int = 0,
    stage2: int = 0,
) -> None:
    today = date.today()
    row = (
        await db.execute(
            select(DealerPlacesUsage).where(
                DealerPlacesUsage.organization_id == organization_id,
                DealerPlacesUsage.usage_date == today,
            )
        )
    ).scalar_one_or_none()
    if row:
        s1 = int(row.stage1_requests or 0) + int(stage1)
        s2 = int(row.stage2_requests or 0) + int(stage2)
        row.stage1_requests = s1
        row.stage2_requests = s2
        row.estimated_usd = estimated_billable_usd(stage1=s1, stage2=s2)
        row.updated_at = datetime.now(timezone.utc)
    else:
        s1 = int(stage1)
        s2 = int(stage2)
        db.add(
            DealerPlacesUsage(
                organization_id=organization_id,
                usage_date=today,
                stage1_requests=s1,
                stage2_requests=s2,
                estimated_usd=estimated_billable_usd(stage1=s1, stage2=s2),
            )
        )
