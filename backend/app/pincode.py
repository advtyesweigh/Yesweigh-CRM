"""India PIN code lookup via India Post–style open API (postalpincode.in), with cache."""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.models import User
from app.state_normalize import canon_state

router = APIRouter(tags=["pincode"])

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 60 * 60 * 24 * 7  # 7 days
_CACHE_MAX = 5000


class PincodeOut(BaseModel):
    pin: str
    city: str = ""
    district: str = ""
    state: str = ""
    office: str = ""


def _normalize_pin(pin: str) -> str:
    digits = "".join(c for c in (pin or "") if c.isdigit())
    return digits


def _cache_get(pin: str) -> dict[str, Any] | None:
    entry = _CACHE.get(pin)
    if not entry:
        return None
    ts, data = entry
    if time.monotonic() - ts > _CACHE_TTL_SEC:
        _CACHE.pop(pin, None)
        return None
    return data


def _cache_set(pin: str, data: dict[str, Any]) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        # Drop oldest ~10%
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: max(1, _CACHE_MAX // 10)]
        for k, _ in oldest:
            _CACHE.pop(k, None)
    _CACHE[pin] = (time.monotonic(), data)


async def lookup_pincode(pin: str) -> PincodeOut:
    pin = _normalize_pin(pin)
    if len(pin) != 6:
        raise HTTPException(400, "PIN must be 6 digits")

    cached = _cache_get(pin)
    if cached is not None:
        return PincodeOut(**cached)

    url = f"https://api.postalpincode.in/pincode/{pin}"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            payload = r.json()
    except Exception as e:
        raise HTTPException(502, f"PIN lookup failed: {e}") from e

    if not isinstance(payload, list) or not payload:
        raise HTTPException(404, "PIN not found")

    block = payload[0] if isinstance(payload[0], dict) else {}
    status = str(block.get("Status") or "")
    offices = block.get("PostOffice") or []
    if status.lower() != "success" or not isinstance(offices, list) or not offices:
        raise HTTPException(404, "PIN not found")

    office = offices[0] if isinstance(offices[0], dict) else {}
    # Prefer Block/Division for city when Name is a small post office; fall back to Name.
    city = (
        str(office.get("Block") or "").strip()
        or str(office.get("Division") or "").strip()
        or str(office.get("Name") or "").strip()
    )
    district = str(office.get("District") or "").strip()
    state = canon_state(str(office.get("State") or "").strip())
    name = str(office.get("Name") or "").strip()

    data = {
        "pin": pin,
        "city": city,
        "district": district,
        "state": state,
        "office": name,
    }
    _cache_set(pin, data)
    return PincodeOut(**data)


@router.get("/api/pincode/{pin}", response_model=PincodeOut)
async def get_pincode(pin: str, _user: User = Depends(get_current_user)):
    return await lookup_pincode(pin)
