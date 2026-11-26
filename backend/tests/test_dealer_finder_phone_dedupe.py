"""Dealer Finder — dedupe eligible places by normalized phone."""
from __future__ import annotations

from types import SimpleNamespace

from app.dealer_finder.jobs import dedupe_places_by_phone


def _place(*, id: int, phone: str, name: str = "", website: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=id, phone=phone, name=name, website=website, address="", maps_url="")


def test_dedupe_keeps_one_row_per_phone_prefers_website():
    rows = [
        _place(id=10, phone="9095060515", name="Kanimozhi A"),
        _place(id=11, phone="9095060515", name="Kanimozhi Scale Company", website="https://example.com"),
    ]
    deduped, removed = dedupe_places_by_phone(rows)  # type: ignore[arg-type]
    assert removed == 1
    assert len(deduped) == 1
    assert deduped[0].id == 11


def test_dedupe_normalizes_country_prefix():
    rows = [
        _place(id=1, phone="+91 90950 60515", name="A"),
        _place(id=2, phone="9095060515", name="B"),
    ]
    deduped, removed = dedupe_places_by_phone(rows)  # type: ignore[arg-type]
    assert removed == 1
    assert len(deduped) == 1