"""Restart / resume recipient selection — never re-blast SUCCESS."""
from __future__ import annotations

from app.paced_media_batch import prepare_recipients_for_resume


def test_prepare_keeps_sent_and_skipped_resets_failed():
    queue, stats = prepare_recipients_for_resume(
        [
            {"to_phone": "911", "status": "sent", "sent_at": "2026-11-22T10:00:00+00:00"},
            {"to_phone": "912", "status": "failed", "error": "Instance is Not Connected"},
            {"to_phone": "913", "status": "skipped", "error": "duplicate"},
            {"to_phone": "914", "status": "pending"},
            {"to_phone": "915", "status": "failed", "error": "timeout"},
        ]
    )
    assert [r["status"] for r in queue] == [
        "sent",
        "pending",
        "skipped",
        "pending",
        "pending",
    ]
    assert queue[1]["error"] == ""
    assert queue[4]["error"] == ""
    assert stats["success_count"] == 1
    assert stats["skipped_count"] == 1
    assert stats["failed_count"] == 0
    assert stats["pending_count"] == 3
    assert stats["next_index"] == 1  # first failed → pending
    assert stats["total_count"] == 5


def test_prepare_all_sent_has_no_pending():
    queue, stats = prepare_recipients_for_resume(
        [
            {"to_phone": "911", "status": "sent"},
            {"to_phone": "912", "status": "skipped"},
        ]
    )
    assert stats["pending_count"] == 0
    assert stats["next_index"] == 2
    assert [r["status"] for r in queue] == ["sent", "skipped"]


def test_prepare_early_disconnect_style_queue():
    """Run 114-style: 0 success, 1 failed at start, rest pending."""
    recipients = [{"to_phone": f"9{i}", "status": "pending"} for i in range(5)]
    recipients[0] = {"to_phone": "90", "status": "failed", "error": "offline"}
    queue, stats = prepare_recipients_for_resume(recipients)
    assert stats["success_count"] == 0
    assert stats["pending_count"] == 5
    assert stats["next_index"] == 0
    assert all(r["status"] == "pending" for r in queue)


def test_prepare_partial_success_like_run_113():
    recipients = (
        [{"to_phone": f"s{i}", "status": "sent"} for i in range(148)]
        + [{"to_phone": f"f{i}", "status": "failed", "error": "x"} for i in range(15)]
        + [{"to_phone": f"k{i}", "status": "skipped"} for i in range(5)]
        + [{"to_phone": f"p{i}", "status": "pending"} for i in range(22)]
    )
    queue, stats = prepare_recipients_for_resume(recipients)
    assert stats["success_count"] == 148
    assert stats["skipped_count"] == 5
    assert stats["pending_count"] == 15 + 22
    assert stats["failed_count"] == 0
    assert stats["next_index"] == 148  # first failed slot
    # Already-sent phones stay sent — no double-blast
    assert all(queue[i]["status"] == "sent" for i in range(148))
