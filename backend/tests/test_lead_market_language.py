from app.lead_market_language import (
    normalize_market_language,
    parse_language_filter,
    market_for_stored_language,
)


def test_normalize_market_odia_aliases():
    assert normalize_market_language("or-IN") == "od"
    assert normalize_market_language("od-IN") == "od"


def test_parse_language_filter_iso():
    assert parse_language_filter("ml-IN") == "ml"
    assert parse_language_filter("hi") == "hi"
    assert parse_language_filter("other") == "other"


def test_market_for_stored_unknown_script():
    assert market_for_stored_language("xx") == "other"
    assert market_for_stored_language("") == ""


def test_normalize_market_assamese():
    assert parse_language_filter("as-IN") == "as"
    assert parse_language_filter("en-IN") == "en"