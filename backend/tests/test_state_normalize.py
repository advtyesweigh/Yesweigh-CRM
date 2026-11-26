from app.state_normalize import (
    canon_state,
    lead_state_bucket,
    state_match_values,
    state_match_values_multi,
)


def test_indic_scripts_merge_to_english():
    assert canon_state("बिहार") == "Bihar"
    assert canon_state("छत्तीसगढ़") == "Chhattisgarh"
    assert canon_state("महाराष्ट्र") == "Maharashtra"
    assert canon_state("दिल्ली") == "Delhi"
    assert canon_state("தமிழ் நாடு") == "Tamil Nadu"
    assert canon_state("தமிழ்நாடு") == "Tamil Nadu"


def test_english_variants_merge():
    assert canon_state("Kerala") == "Keralam"
    assert canon_state("Orissa") == "Odisha"
    assert lead_state_bucket("Jammu & Kashmir") == "Jammu and Kashmir"


def test_state_match_includes_indic():
    vals = state_match_values("Bihar")
    assert "Bihar" in vals
    assert "बिहार" in vals


def test_state_match_multi_pipe():
    vals = state_match_values_multi("Tamil Nadu|Puducherry")
    assert "Tamil Nadu" in vals or any("Tamil" in v for v in vals)
    assert "Puducherry" in vals or any("Puducherry" in v for v in vals)