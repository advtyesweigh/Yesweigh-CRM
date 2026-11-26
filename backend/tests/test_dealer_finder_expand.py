"""Unit tests for Dealer Finder district→city expansion path."""

from app.dealer_finder.geo_plan import expand_search_units, _normalize_plan_districts
from app.dealer_finder.india_geo import districts_for_state
from app.dealer_finder.normalize import identity_hash, search_identity, validate_website_url


def test_tamil_nadu_verified_districts():
    districts = districts_for_state("Tamil Nadu")
    assert len(districts) >= 30
    assert "Chennai" in districts
    assert "Coimbatore" in districts


def test_expand_district_and_city_units():
    plan = {
        "districts": [
            {"name": "Chennai", "cities": ["T. Nagar", "Guindy"]},
            {"name": "Coimbatore", "cities": ["Peelamedu"]},
        ]
    }
    cats = ["weighing scale dealers", "weighbridge dealers"]
    units = expand_search_units(plan, categories=cats, state="Tamil Nadu", search_depth="full")
    # 2 districts × 2 cats + (2+1) cities × 2 cats = 4 + 6 = 10
    assert len(units) == 10
    kinds = {u["kind"] for u in units}
    assert kinds == {"district", "city"}
    assert any("in Chennai Tamil Nadu India" in u["text_query"] for u in units)
    assert any("in T. Nagar Chennai Tamil Nadu India" in u["text_query"] for u in units)


def test_expand_district_only():
    plan = {"districts": [{"name": "Salem", "cities": ["Attur"]}]}
    units = expand_search_units(
        plan, categories=["weighing scale dealers"], state="Tamil Nadu", search_depth="district_only"
    )
    assert len(units) == 1
    assert units[0]["kind"] == "district"
    assert units[0]["city"] == ""


def test_merge_verified_districts_not_dropped():
    verified = ["Chennai", "Coimbatore", "Madurai"]
    gemini = [{"name": "Chennai", "cities": ["T. Nagar"]}, {"name": "Extra Dist", "cities": ["X"]}]
    merged = _normalize_plan_districts(gemini, verified)
    names = [d["name"] for d in merged]
    assert "Chennai" in names and "Coimbatore" in names and "Madurai" in names
    chennai = next(d for d in merged if d["name"] == "Chennai")
    assert "T. Nagar" in chennai["cities"]


def test_identity_hash_stable():
    a = search_identity(state="Tamil Nadu", district="Chennai", city="Guindy", category="weighing scale dealers")
    b = search_identity(state="tamil nadu", district="chennai", city="guindy", category="Weighing Scale Dealers")
    assert identity_hash(a) == identity_hash(b)


def test_validate_website():
    assert validate_website_url("https://example.com/path") == "https://example.com/path"
    assert validate_website_url("example.com") == "https://example.com"
    assert validate_website_url("") == ""
    assert validate_website_url("not a url") == ""
