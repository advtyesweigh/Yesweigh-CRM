"""Extract lead fields from a visiting-card photo via Gemini vision + Google Places."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.dealer_finder.places import _extract_district, _extract_state
from app.enrich_leads_places import extract_city, search_place
from app.google_places_search import places_api_key, resolve_state
from app.media_store import PUBLIC_PREFIX, resolve_path
from app.phone_util import normalize_in_mobile
from app.state_normalize import canon_state

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT = """You are reading an Indian business visiting / business card photo for a CRM.

Extract every legible field. Return ONLY valid JSON (no markdown) with this shape:
{
  "business_name": "",
  "contact_persons": [],
  "phones": [],
  "address": "",
  "city": "",
  "district": "",
  "state": "",
  "pin": "",
  "tagline": "",
  "email": "",
  "website": "",
  "notes": "",
  "raw_text": ""
}

Rules:
- business_name: company / shop name (largest brand text), not a person.
- contact_persons: person names on the card (may be multiple).
- phones: all phone / mobile / landline numbers as digits (keep leading 0 if shown; include country code if present).
- address: full address line(s) as printed.
- city / district / state / pin: only if clearly present or strongly implied by the address.
- For Indian addresses like "Chinna Bazaar, Nellore" set city/area and district when obvious; leave state blank if not printed.
- tagline: services / slogan / authorization line.
- raw_text: concise OCR dump of all readable text.
- Use empty string or [] when unknown. Do not invent values.
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def _parse_json_blob(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _mime_from_name(name: str) -> str:
    lower = (name or "").lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def load_image_from_url(image_url: str) -> tuple[bytes, str]:
    """Load WA media from disk (preferred) or reject remote URLs."""
    raw = (image_url or "").strip()
    if not raw:
        raise ValueError("image_url is required")
    path_part = raw
    if "://" in raw:
        path_part = urlparse(raw).path or ""
    if PUBLIC_PREFIX in path_part:
        filename = path_part.rsplit("/", 1)[-1].split("?")[0]
        disk = resolve_path(filename)
        if not disk:
            raise FileNotFoundError("WhatsApp media file not found")
        blob = disk.read_bytes()
        if not blob:
            raise ValueError("Empty media file")
        if len(blob) > 3_500_000:
            raise ValueError("Image too large for AI extract (max ~3.5MB)")
        return blob, _mime_from_name(disk.name)
    raise ValueError("Only /api/whatsapp/media/… URLs are supported")


def load_image_from_base64(data_b64: str, mime: str = "image/jpeg") -> tuple[bytes, str]:
    raw = (data_b64 or "").strip()
    if raw.startswith("data:") and "," in raw:
        header, raw = raw.split(",", 1)
        if "image/" in header:
            mime = header.split(";")[0].split(":", 1)[-1].strip() or mime
    try:
        blob = base64.b64decode(raw, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid image_base64") from exc
    if not blob:
        raise ValueError("Empty image_base64")
    if len(blob) > 3_500_000:
        raise ValueError("Image too large for AI extract (max ~3.5MB)")
    return blob, (mime or "image/jpeg").strip() or "image/jpeg"


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;/|]+", value)
        return [p.strip() for p in parts if p and str(p).strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            s = str(item or "").strip()
            if s and s not in out:
                out.append(s)
        return out
    s = str(value).strip()
    return [s] if s else []


def _normalize_phones(phones: list[str]) -> list[str]:
    out: list[str] = []
    for p in phones:
        digits = normalize_in_mobile(p)
        if digits:
            if digits not in out:
                out.append(digits)
            continue
        cleaned = re.sub(r"[^\d+]", "", p).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned[:20])
    return out


def _normalize_extracted(data: dict[str, Any]) -> dict[str, Any]:
    business = str(data.get("business_name") or data.get("company") or "").strip()
    contacts = _as_str_list(data.get("contact_persons") or data.get("contact_person"))
    phones = _normalize_phones(_as_str_list(data.get("phones") or data.get("phone")))
    address = str(data.get("address") or "").strip()
    city = str(data.get("city") or "").strip()
    district = str(data.get("district") or "").strip()
    state_raw = str(data.get("state") or "").strip()
    state = canon_state(state_raw) if state_raw else ""
    if state_raw and not state:
        label, _ = resolve_state(state_raw)
        state = canon_state(label) or label
    pin = "".join(c for c in str(data.get("pin") or "") if c.isdigit())[:20]
    tagline = str(data.get("tagline") or "").strip()
    email = str(data.get("email") or "").strip()
    website = str(data.get("website") or "").strip()
    notes = str(data.get("notes") or "").strip()
    raw_text = str(data.get("raw_text") or "").strip()
    return {
        "business_name": business,
        "contact_persons": contacts,
        "phones": phones,
        "address": address,
        "city": city,
        "district": district,
        "state": state,
        "pin": pin,
        "tagline": tagline,
        "email": email,
        "website": website,
        "notes": notes,
        "raw_text": raw_text,
    }


async def gemini_extract_card(image: bytes, mime: str) -> tuple[dict[str, Any], str]:
    settings = get_settings()
    api_key = (settings.gemini_api_key or "").strip()
    model = (settings.ai_model or "gemini-2.5-flash-lite").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    def _call() -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        parts = [
            types.Part.from_text(text=_EXTRACT_PROMPT),
            types.Part.from_bytes(data=image, mime_type=mime or "image/jpeg"),
        ]
        resp = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )
        return (getattr(resp, "text", None) or "") or ""

    text = await asyncio.to_thread(_call)
    return _normalize_extracted(_parse_json_blob(text)), model


def _places_query_from_fields(fields: dict[str, Any]) -> str:
    bits = [
        str(fields.get("business_name") or "").strip(),
        str(fields.get("address") or "").strip(),
        str(fields.get("city") or "").strip(),
        str(fields.get("district") or "").strip(),
        str(fields.get("state") or "").strip(),
        "India",
    ]
    # Dedupe while preserving order
    seen: set[str] = set()
    parts: list[str] = []
    for b in bits:
        key = b.lower()
        if not b or key in seen:
            continue
        # Skip if already contained in a longer prior part
        if any(key in p.lower() for p in parts):
            continue
        seen.add(key)
        parts.append(b)
    return ", ".join(parts) if parts else "India"


async def resolve_geo_via_places(fields: dict[str, Any]) -> dict[str, Any]:
    """Fill missing state/district (and city when empty) via Places Text Search."""
    need_state = not str(fields.get("state") or "").strip()
    need_district = not str(fields.get("district") or "").strip()
    if not need_state and not need_district:
        return {
            "places_resolved": False,
            "places_query": "",
            "places_note": "state and district already present",
        }

    key = places_api_key()
    if not key:
        return {
            "places_resolved": False,
            "places_query": "",
            "places_note": "GOOGLE_MAPS_API_KEY not configured",
        }

    query = _places_query_from_fields(fields)
    if len(query) < 8:
        return {
            "places_resolved": False,
            "places_query": query,
            "places_note": "address too short for Places lookup",
        }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            places = await search_place(client, key, query)
            # Prefer address-only query if business name skewed results
            if not places and fields.get("address"):
                addr_q = ", ".join(
                    p
                    for p in (
                        str(fields.get("address") or "").strip(),
                        str(fields.get("city") or "").strip(),
                        "India",
                    )
                    if p
                )
                places = await search_place(client, key, addr_q)
                query = addr_q
    except Exception as exc:  # noqa: BLE001
        logger.warning("Places resolve for visiting card failed: %s", exc)
        return {
            "places_resolved": False,
            "places_query": query,
            "places_note": f"Places error: {type(exc).__name__}",
        }

    if not places:
        return {
            "places_resolved": False,
            "places_query": query,
            "places_note": "no Places results",
        }

    place = places[0]
    comps = place.get("addressComponents") or []
    state = _extract_state(comps, "")
    district = _extract_district(comps, "")
    city = extract_city(comps, district or state)
    if state:
        label, _ = resolve_state(state)
        state = canon_state(label) or label or state
    updated = False
    if need_state and state:
        fields["state"] = state
        updated = True
    if need_district and district:
        fields["district"] = district
        updated = True
    if not str(fields.get("city") or "").strip() and city:
        fields["city"] = city
        updated = True
    if not str(fields.get("address") or "").strip():
        fmt = str(place.get("formattedAddress") or "").strip()
        if fmt:
            fields["address"] = fmt
            updated = True

    return {
        "places_resolved": updated,
        "places_query": query,
        "places_note": "ok" if updated else "Places hit but nothing new to fill",
        "places_formatted_address": str(place.get("formattedAddress") or "").strip(),
    }


def build_lead_prefill(fields: dict[str, Any], *, source: str = "Visiting Card") -> dict[str, Any]:
    business = str(fields.get("business_name") or "").strip()
    contacts = list(fields.get("contact_persons") or [])
    contact_person = ", ".join(contacts) if contacts else ""
    phones = list(fields.get("phones") or [])
    phone = phones[0] if phones else ""
    name = business or (contacts[0] if contacts else "")
    note_bits = [
        str(fields.get("tagline") or "").strip(),
        str(fields.get("notes") or "").strip(),
    ]
    if len(contacts) > 1:
        note_bits.append(f"Contacts: {', '.join(contacts)}")
    if len(phones) > 1:
        note_bits.append(f"Other phones: {', '.join(phones[1:])}")
    if fields.get("email"):
        note_bits.append(f"Email: {fields['email']}")
    if fields.get("website"):
        note_bits.append(f"Website: {fields['website']}")
    if fields.get("raw_text"):
        note_bits.append(f"Card text:\n{fields['raw_text']}")
    notes = "\n".join(b for b in note_bits if b)
    return {
        "name": name,
        "company": business,
        "phone": phone,
        "contact_person": contact_person,
        "address": str(fields.get("address") or "").strip(),
        "city": str(fields.get("city") or "").strip(),
        "district": str(fields.get("district") or "").strip(),
        "state": str(fields.get("state") or "").strip(),
        "pin": str(fields.get("pin") or "").strip(),
        "source": source,
        "status": "new",
        "priority": "medium",
        "notes": notes,
        "tags": ["Visiting Card"],
    }


async def extract_visiting_card(
    *,
    image_url: str = "",
    image_base64: str = "",
    mime: str = "image/jpeg",
    resolve_places: bool = True,
    source: str = "Visiting Card",
) -> dict[str, Any]:
    if image_base64.strip():
        blob, used_mime = load_image_from_base64(image_base64, mime)
    elif image_url.strip():
        blob, used_mime = load_image_from_url(image_url)
    else:
        raise ValueError("Provide image_url or image_base64")

    fields, model = await gemini_extract_card(blob, used_mime)
    places_meta: dict[str, Any] = {
        "places_resolved": False,
        "places_query": "",
        "places_note": "skipped",
    }
    if resolve_places:
        places_meta = await resolve_geo_via_places(fields)

    prefill = build_lead_prefill(fields, source=source)
    return {
        **fields,
        **places_meta,
        "model": model,
        "lead_prefill": prefill,
    }
