"""Move-to knowledge base: phrase extraction + scoring."""
from app.intel.stage_kb import (
    StageKbPatterns,
    best_kb_stage,
    extract_move_features,
    score_blob_against_kb,
    text_matches_exclude,
)


def test_extract_phrases_from_bilingual_waiting_chat():
    messages = [
        {"direction": "out", "body": "hi sir our sales person biju will contact you sir"},
        {"direction": "in", "body": "Okay Sir"},
        {"direction": "in", "body": "No body is call to me Sir", "transcript": "എന്നോട് ആരും വിളിച്ചിട്ടില്ല സർ"},
        {"direction": "in", "body": "Waiting for requirement Sir", "transcript": "ആവശ്യകതയ്ക്കായി കാത്തിരിക്കുകയാണ് സർ"},
    ]
    feat = extract_move_features(messages, to_stage="waiting")
    assert feat.snippet
    assert feat.inbound
    joined = " ".join(feat.phrases).lower()
    assert "waiting for requirement" in joined or any("requirement" in p for p in feat.phrases)
    assert "hint:waiting" in feat.signals or "hint:enquiry" in feat.signals


def test_closed_move_keeps_blast_phrases():
    messages = [
        {"direction": "out", "body": "Dear Customer, due to container strike we will resume dispatching soon"},
    ]
    feat = extract_move_features(messages, to_stage="closed")
    assert feat.phrases
    assert any("dear customer" in p or "container strike" in p for p in feat.phrases)
    assert "hint:closed" in feat.signals


def test_kb_score_prefers_human_stage():
    patterns = StageKbPatterns(
        positive={
            "waiting": [("waiting for requirement", 3), ("nobody is call", 2)],
            "enquiry": [("dealer price", 2)],
        },
        negative={"enquiry": [("waiting for requirement", 2)]},
        exclude=[("dear customer", 2)],
    )
    blob = "waiting for requirement sir. nobody is call to me"
    scores = score_blob_against_kb(blob, patterns)
    assert scores.get("waiting", 0) > scores.get("enquiry", 0)
    assert best_kb_stage(blob, patterns) == "waiting"
    assert text_matches_exclude("Dear Customer — strike notice", patterns) is True
    assert text_matches_exclude("need dealer price for platform", patterns) is False


def test_stop_phrases_filtered():
    feat = extract_move_features(
        [{"direction": "in", "body": "Okay Sir"}, {"direction": "in", "body": "yes"}],
        to_stage="enquiry",
    )
    assert all(p not in {"okay sir", "ok sir", "yes"} for p in feat.phrases)
