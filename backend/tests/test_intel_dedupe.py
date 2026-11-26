from app.intel.dedupe import dedupe_intel_rows, is_blast_summary, summary_quality


def test_blast_summary_detected():
    assert is_blast_summary(
        "Dear Customer, the container strike is over. We will resume dispatching goods from Tuesday."
    )
    assert not is_blast_summary("2 min sir രണ്ട് മിനിറ്റ്")


def test_dedupe_one_row_per_phone_prefers_meaningful_summary():
    rows = [
        {
            "id": "a1",
            "conversation_id": "c1",
            "phone": "919895339647",
            "summary": "Dear Customer, the container strike is over.",
            "age_seconds": 100,
        },
        {
            "id": "a2",
            "conversation_id": "c1",
            "phone": "919895339647",
            "summary": "2 min sir രണ്ട് മിനിറ്റ്",
            "age_seconds": 100,
        },
        {
            "id": "a3",
            "conversation_id": "c1",
            "phone": "919895339647",
            "summary": "Voice message കണ്ടുനോക്കൂ.",
            "age_seconds": 100,
        },
        {
            "id": "b1",
            "conversation_id": "c2",
            "phone": "919567566831",
            "summary": "Voice message Marvel",
            "age_seconds": 40,
        },
    ]
    out = dedupe_intel_rows(rows, drop_blast=True)
    assert len(out) == 2
    royal = next(r for r in out if r["phone"] == "919895339647")
    assert "2 min" in royal["summary"]
    assert summary_quality("2 min sir") > summary_quality("Dear Customer, the container strike is over.")
