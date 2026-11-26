"""Unit tests for visiting-card extract helpers (no live Gemini/Places)."""
from __future__ import annotations

from app.visiting_card import _normalize_extracted, build_lead_prefill, _places_query_from_fields


def test_normalize_and_prefill():
    fields = _normalize_extracted(
        {
            "business_name": "SRI VENKATA SAI WEIGHING SERVICES",
            "contact_persons": ["M. Chinna Bujji", "M. Charan"],
            "phones": ["83747 91703", "+91 8374791703"],
            "address": "13/125, Kakarla Vari Street, Chinna Bazaar, Nellore.",
            "city": "Nellore",
            "district": "",
            "state": "",
            "tagline": "All Types of Electronic Weighing scales Service & Stamping",
        }
    )
    assert fields["business_name"].startswith("SRI VENKATA")
    assert fields["phones"] == ["8374791703"]
    assert len(fields["contact_persons"]) == 2

    prefill = build_lead_prefill(fields)
    assert prefill["company"] == fields["business_name"]
    assert prefill["phone"] == "8374791703"
    assert prefill["source"] == "Visiting Card"
    assert "Visiting Card" in (prefill.get("tags") or [])
    assert "Chinna Bazaar" in prefill["address"]


def test_places_query_includes_address():
    q = _places_query_from_fields(
        {
            "business_name": "SRI VENKATA SAI WEIGHING SERVICES",
            "address": "Chinna Bazaar, Nellore",
            "city": "Nellore",
            "district": "",
            "state": "",
        }
    )
    assert "Nellore" in q
    assert "India" in q
