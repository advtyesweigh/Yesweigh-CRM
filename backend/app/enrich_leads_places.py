"""Enrich LMOMS leads missing phone/city via Google Places API (New).

Uses Text Search with query \"{name} {district} Kerala India\", regionCode IN.
Updates phone + city only when display name confidence is reasonable.
Stores formatted address in notes as `[Google Places] …` when found.

  docker exec yesweigh-crm-api python -m app.enrich_leads_places --dry-run --limit 5
  docker exec yesweigh-crm-api python -m app.enrich_leads_places --limit 50
  docker exec yesweigh-crm-api python -m app.enrich_leads_places

Env: GOOGLE_MAPS_API_KEY or GOOGLE_PLACES_API_KEY (never commit).
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import or_, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Lead
from app.phone_util import normalize_in_mobile

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.addressComponents",
    ]
)

_NOISE = re.compile(
    r"\b(pvt|private|ltd|limited|llp|co|company|enterprises?|traders?|"
    r"trading|agency|agencies|services?|service|sales?|scale|scales|"
    r"meters?|meter|works|international|tech|solution|solutions|"
    r"and|&)\b",
    re.I,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def _norm_tokens(value: str) -> set[str]:
    s = (value or "").lower()
    s = _NOISE.sub(" ", s)
    s = _NON_ALNUM.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return {t for t in s.split(" ") if len(t) >= 2}


_UNLIKELY = re.compile(
    r"\b(clinic|hospital|doctor|dr\.?|advocate|adv\.?|lawyer|notary|"
    r"skin|dental|ayurved|temple|church|mosque|school|college|"
    r"courier|hotel|restaurant|cafe|bank|atm)\b",
    re.I,
)
_SCALEISH = re.compile(
    r"\b(scale|scales|weigh|weighing|meter|meters|balance)\b",
    re.I,
)


def name_match_score(lead_name: str, place_name: str) -> float:
    """Token Jaccard after dropping noisy business words. 1.0 = strong."""
    # Reject obvious wrong verticals unless the lead itself looks like that.
    if _UNLIKELY.search(place_name or "") and not _UNLIKELY.search(lead_name or ""):
        return 0.0
    a = _norm_tokens(lead_name)
    b = _norm_tokens(place_name)
    if not a or not b:
        # Fall back to raw lowercase containment for short personal names.
        ln = _WS.sub(" ", _NON_ALNUM.sub(" ", (lead_name or "").lower())).strip()
        pn = _WS.sub(" ", _NON_ALNUM.sub(" ", (place_name or "").lower())).strip()
        if not ln or not pn:
            return 0.0
        if ln == pn:
            return 0.9
        if ln in pn or pn in ln:
            return 0.7
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    union = a | b
    jaccard = len(inter) / len(union)
    # Require most lead tokens to appear (avoid weak one-token hits).
    coverage = len(inter) / len(a)
    score = min(1.0, 0.55 * jaccard + 0.45 * coverage)
    # One shared token of a multi-token personal name is too weak.
    if len(a) >= 2 and len(inter) == 1 and coverage < 0.67:
        score = min(score, 0.35)
    # Soft boost when both look like weighing/scale businesses.
    if _SCALEISH.search(lead_name or "") and _SCALEISH.search(place_name or ""):
        score = min(1.0, score + 0.1)
    return score



def district_in_place(district: str, formatted_address: str, components: list[dict[str, Any]]) -> bool:
    d = (district or "").strip().lower()
    if not d:
        return True
    addr = (formatted_address or "").lower()
    if d in addr:
        return True
    # Kozhikode / Kozhikkode spelling variants
    variants = {d, d.replace("kk", "k"), d.replace("zhikk", "zhik")}
    for c in components or []:
        text = str(c.get("longText") or c.get("shortText") or "").lower()
        if text in variants or any(v in text for v in variants if len(v) >= 4):
            return True
        if any(v in addr for v in variants if len(v) >= 4):
            return True
    return False


def extract_city(components: list[dict[str, Any]], district: str) -> str:
    by_type: dict[str, str] = {}
    for c in components or []:
        types = c.get("types") or []
        text = str(c.get("longText") or c.get("shortText") or "").strip()
        if not text:
            continue
        for t in types:
            by_type.setdefault(str(t), text)
    for key in (
        "locality",
        "postal_town",
        "sublocality_level_1",
        "sublocality",
        "administrative_area_level_3",
    ):
        val = by_type.get(key, "").strip()
        if not val:
            continue
        # Prefer a locality that isn't just the district name.
        if val.lower() == (district or "").strip().lower():
            continue
        return val[:80]
    # Fall back to locality even if it equals district.
    for key in ("locality", "administrative_area_level_2"):
        val = by_type.get(key, "").strip()
        if val:
            return val[:80]
    return ""


def format_phone(national: str, international: str) -> str:
    raw = (national or international or "").strip()
    if not raw:
        return ""
    digits = normalize_in_mobile(raw)
    if digits:
        return digits
    # Keep truncated national string if not a standard mobile.
    return raw[:40]


def places_note(formatted_address: str) -> str:
    addr = (formatted_address or "").strip()
    if not addr:
        return ""
    return f"[Google Places] {addr}"


@dataclass
class Stats:
    scanned: int = 0
    updated: int = 0
    skipped_low_confidence: int = 0
    zero_results: int = 0
    no_phone: int = 0
    errors: int = 0
    samples: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


async def search_place(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
) -> list[dict[str, Any]]:
    resp = await client.post(
        PLACES_SEARCH_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        json={
            "textQuery": query,
            "regionCode": "IN",
            "maxResultCount": 5,
            "languageCode": "en",
        },
    )
    if resp.status_code == 400 and "ZERO" in resp.text.upper():
        return []
    resp.raise_for_status()
    data = resp.json()
    return list(data.get("places") or [])


def pick_best(
    lead_name: str,
    district: str,
    places: list[dict[str, Any]],
    *,
    min_score: float = 0.45,
) -> tuple[dict[str, Any] | None, float, str]:
    best: dict[str, Any] | None = None
    best_score = 0.0
    best_reason = "no_candidates"
    for p in places:
        display = ((p.get("displayName") or {}).get("text")) or ""
        score = name_match_score(lead_name, display)
        addr = p.get("formattedAddress") or ""
        comps = p.get("addressComponents") or []
        if score < min_score:
            if score > best_score:
                best_score = score
                best_reason = f"low_score:{score:.2f}:{display[:60]}"
            continue
        if not district_in_place(district, addr, comps):
            if score > best_score:
                best_score = score
                best_reason = f"district_mismatch:{display[:60]}"
            continue
        if score > best_score:
            best = p
            best_score = score
            best_reason = "ok"
    return best, best_score, best_reason


async def enrich(
    *,
    limit: int | None,
    dry_run: bool,
    delay_s: float,
    min_score: float,
    only_ids: list[int] | None,
) -> Stats:
    settings = get_settings()
    api_key = (settings.google_maps_api_key or settings.google_places_api_key or "").strip()
    if not api_key:
        raise SystemExit(
            "Missing GOOGLE_MAPS_API_KEY / GOOGLE_PLACES_API_KEY in environment"
        )

    stats = Stats()
    async with SessionLocal() as db:
        stmt = (
            select(Lead)
            .where(Lead.name != "")
            .where(Lead.district != "")
            .where(or_(Lead.phone == "", Lead.city == ""))
            .order_by(Lead.id.desc())
        )
        if only_ids:
            stmt = stmt.where(Lead.id.in_(only_ids))
        if limit is not None:
            stmt = stmt.limit(limit)
        leads = list((await db.execute(stmt)).scalars().all())

        async with httpx.AsyncClient(timeout=30.0) as client:
            for lead in leads:
                stats.scanned += 1
                from app.state_normalize import canon_state

                state = canon_state((lead.state or "Keralam").strip() or "Keralam")
                query = f"{lead.name} {lead.district} {state} India"
                try:
                    places = await search_place(client, api_key, query)
                except httpx.HTTPStatusError as exc:
                    stats.errors += 1
                    msg = f"id={lead.id} HTTP {exc.response.status_code}"
                    stats.failures.append(msg)
                    print(f"ERR {msg}", flush=True)
                    await asyncio.sleep(delay_s)
                    continue
                except Exception as exc:  # noqa: BLE001
                    stats.errors += 1
                    msg = f"id={lead.id} {type(exc).__name__}"
                    stats.failures.append(msg)
                    print(f"ERR {msg}: {exc}", flush=True)
                    await asyncio.sleep(delay_s)
                    continue

                if not places:
                    stats.zero_results += 1
                    stats.failures.append(f"id={lead.id} ZERO_RESULTS {lead.name!r}")
                    print(f"ZERO id={lead.id} {lead.name}", flush=True)
                    await asyncio.sleep(delay_s)
                    continue

                best, score, reason = pick_best(
                    lead.name, lead.district, places, min_score=min_score
                )
                if best is None:
                    stats.skipped_low_confidence += 1
                    stats.failures.append(
                        f"id={lead.id} skip:{reason} {lead.name!r}"
                    )
                    print(f"SKIP id={lead.id} {reason} {lead.name}", flush=True)
                    await asyncio.sleep(delay_s)
                    continue

                display = ((best.get("displayName") or {}).get("text")) or ""
                addr = (best.get("formattedAddress") or "").strip()
                comps = best.get("addressComponents") or []
                phone = format_phone(
                    best.get("nationalPhoneNumber") or "",
                    best.get("internationalPhoneNumber") or "",
                )
                city = extract_city(comps, lead.district)

                changed = False
                if phone and not (lead.phone or "").strip():
                    lead.phone = phone
                    changed = True
                elif not phone:
                    stats.no_phone += 1

                if city and not (lead.city or "").strip():
                    lead.city = city
                    changed = True

                if addr and not (lead.address or "").strip():
                    lead.address = addr
                    changed = True

                sample = (
                    f"id={lead.id} {lead.name} | phone={lead.phone or '—'} | "
                    f"city={lead.city or '—'} | score={score:.2f} | place={display[:50]}"
                )
                if changed:
                    stats.updated += 1
                    if len(stats.samples) < 20:
                        stats.samples.append(sample)
                    print(("DRY " if dry_run else "OK  ") + sample, flush=True)
                else:
                    stats.skipped_low_confidence += 1
                    print(f"NOCHG {sample}", flush=True)

                if stats.scanned % 25 == 0:
                    print(
                        f"... progress scanned={stats.scanned} updated={stats.updated} "
                        f"zero={stats.zero_results} skip={stats.skipped_low_confidence} "
                        f"err={stats.errors}",
                        flush=True,
                    )
                    if not dry_run:
                        await db.commit()

                await asyncio.sleep(delay_s)

        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Enrich leads via Google Places API")
    p.add_argument("--limit", type=int, default=None, help="Max leads to process")
    p.add_argument("--dry-run", action="store_true", help="Do not commit DB writes")
    p.add_argument("--delay", type=float, default=0.25, help="Seconds between API calls")
    p.add_argument("--min-score", type=float, default=0.45, help="Min name match score")
    p.add_argument(
        "--ids",
        type=str,
        default="",
        help="Comma-separated lead ids (optional)",
    )
    args = p.parse_args(argv)
    only_ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] or None

    t0 = time.time()
    stats = asyncio.run(
        enrich(
            limit=args.limit,
            dry_run=args.dry_run,
            delay_s=max(0.05, args.delay),
            min_score=args.min_score,
            only_ids=only_ids,
        )
    )
    elapsed = time.time() - t0
    print("---", flush=True)
    print(
        f"scanned={stats.scanned} updated={stats.updated} "
        f"zero_results={stats.zero_results} low_confidence={stats.skipped_low_confidence} "
        f"matched_no_phone={stats.no_phone} errors={stats.errors} "
        f"dry_run={args.dry_run} elapsed_s={elapsed:.1f}",
        flush=True,
    )
    if stats.samples:
        print("samples:", flush=True)
        for s in stats.samples:
            print(f"  {s}", flush=True)
    return 0 if stats.errors == 0 or stats.updated > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
