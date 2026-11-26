from app.lead_state_market import (
    canon_states_for_market,
    market_language_for_canon_state,
)


def test_market_language_kerala_malayalam():
    assert market_language_for_canon_state("Keralam") == "ml"


def test_market_language_andaman_tamil():
    assert market_language_for_canon_state("Andaman and Nicobar") == "ta"


def test_market_language_goa_marathi():
    assert market_language_for_canon_state("Goa") == "mr"


def test_northeast_assamese_market():
    assert market_language_for_canon_state("Assam") == "as"
    assert market_language_for_canon_state("Sikkim") == "as"


def test_assamese_states():
    ne = set(canon_states_for_market("as"))
    assert "Assam" in ne
    assert "Tripura" in ne
    assert "Arunachal Pradesh" in ne


def test_hindi_belt_includes_delhi_and_jk():
    hi = set(canon_states_for_market("hi"))
    assert "Delhi" in hi
    assert "Jammu and Kashmir" in hi
    assert "Ladakh" in hi


def test_malayalam_states():
    assert set(canon_states_for_market("ml")) == {"Keralam", "Lakshadweep"}