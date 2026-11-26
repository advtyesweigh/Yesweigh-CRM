"""Unit tests for Google Places state resolve + CRM 'new' matching (no live API)."""
from __future__ import annotations

from app.google_places_search import (
    CrmContact,
    build_text_query,
    crm_match_reason,
    default_weighing_places_keyword,
    places_keyword_was_coerced,
    resolve_state,
)


def test_resolve_up_aliases():
    label, vals = resolve_state("UP")
    assert label == "Uttar Pradesh"
    assert any("Uttar" in v for v in vals)

    label2, _ = resolve_state("utharpradesh")
    assert label2 == "Uttar Pradesh"


def test_generic_dealers_keyword_coerced():
    assert default_weighing_places_keyword("dealers") == "weighing scale dealers"
    assert default_weighing_places_keyword("dealer") == "weighing scale dealers"
    assert default_weighing_places_keyword("distributors") == "weighing scale dealers"
    assert default_weighing_places_keyword("car dealers") == "weighing scale dealers"
    assert default_weighing_places_keyword("new dealers") == "weighing scale dealers"
    assert default_weighing_places_keyword("electronic weighing scale") == "electronic weighing scale"
    assert not places_keyword_was_coerced("electronic weighing scale", "electronic weighing scale")
    assert places_keyword_was_coerced("dealers", "weighing scale dealers")


def test_build_text_query():
    q = build_text_query("weighing scale dealers", "UP")
    assert "Uttar Pradesh" in q
    assert "weighing scale" in q.lower()


def test_build_text_query_with_city():
    q = build_text_query("weighing scale dealers", "Tamil Nadu", city="Coimbatore")
    assert "Coimbatore" in q
    assert "Tamil Nadu" in q


def test_tamilnadu_alias():
    label, _ = resolve_state("tamilnadu")
    assert label == "Tamil Nadu"


def test_crm_match_by_phone():
    crm = [
        CrmContact(name="Acme Scales", city="Lucknow", phone10="9876543210", kind="dealer", record_id=1)
    ]
    row = {"name": "Other Name", "city": "Kanpur", "phone": "+91 98765 43210"}
    matched, reason = crm_match_reason(row, crm)
    assert matched
    assert reason.startswith("phone:")


def test_crm_match_by_name_city():
    crm = [
        CrmContact(
            name="Meezan Weighing Scales",
            city="Lucknow",
            phone10="",
            kind="lead",
            record_id=9,
        )
    ]
    row = {
        "name": "Meezan Weighing Scales Pvt Ltd",
        "city": "Lucknow",
        "phone": "",
    }
    matched, reason = crm_match_reason(row, crm)
    assert matched
    assert "name_city" in reason or reason.startswith("name:")


def test_new_when_no_match():
    crm = [
        CrmContact(name="Existing Dealer", city="Agra", phone10="9111111111", kind="dealer", record_id=2)
    ]
    row = {"name": "Brand New Scales Co", "city": "Varanasi", "phone": "9000000000"}
    matched, _ = crm_match_reason(row, crm)
    assert not matched
