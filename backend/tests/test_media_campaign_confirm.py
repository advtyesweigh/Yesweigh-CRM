"""Pending media confirm binding + paced hours parsing (no DB)."""
from __future__ import annotations

from app.ai.service import (
    _campaign_status_override,
    _extract_pending_media_campaign,
    _is_user_confirm,
    _paced_media_started_reply,
)
from app.paced_media_batch import parse_total_hours


def test_is_user_confirm_phrases():
    assert _is_user_confirm("confirm")
    assert _is_user_confirm("Yes")
    assert _is_user_confirm("go ahead")
    assert _is_user_confirm("start now")
    assert not _is_user_confirm("confirm with caption hello")
    assert not _is_user_confirm("Send media UID 1000")


def test_extract_pending_media_from_assistant_summary():
    history = [
        {
            "role": "assistant",
            "content": (
                "- **FROM:** Visakh\n"
                "- **TO:** 401 recipients in Audience 12 (**Leads Kerala**)\n"
                "- **MEDIA:** UID 1000 (**GATC RAM**, video, ~30.1 MB)\n"
                "- **WINDOW:** Paced over **36 hours** (background worker)\n\n"
                "Please confirm if you want me to start this campaign now."
            ),
        }
    ]
    pending = _extract_pending_media_campaign(history)
    assert pending is not None
    assert pending["uid"] == 1000
    assert pending["audience_uid"] == 12
    assert pending["total_hours"] == 36
    assert "Visakh" in pending["from_agent_name"]


def test_parse_total_hours_keeps_36():
    assert parse_total_hours({"total_hours": 36}) == 36
    assert parse_total_hours({"total_hours": 24}) == 24
    assert parse_total_hours({"total_hours": 48}) == 48
    assert parse_total_hours({"total_hours": 30}) == 30
    assert parse_total_hours({"total_hours": 6}) == 24
    assert parse_total_hours({}) is None


def test_paced_started_reply_not_completed():
    reply = _paced_media_started_reply(
        {
            "total_hours": 36,
            "recipient_count": 401,
            "from_employee_name": "Visakh",
            "media_title": "GATC RAM",
            "campaign_run_id": 9,
        }
    )
    assert "started" in reply.lower()
    assert "running" in reply.lower()
    assert "completed" not in reply.lower()
    assert "Report #9" in reply


def test_campaign_status_override_from_tool_results():
    tools = [
        {
            "ok": True,
            "tool": "send_whatsapp_media_batch",
            "result": {
                "paced": True,
                "scheduled": True,
                "total_hours": 36,
                "recipient_count": 401,
                "from_employee_name": "Visakh",
                "media_title": "GATC RAM",
                "campaign_run_id": 12,
            },
        }
    ]
    out = _campaign_status_override(tools)
    assert out is not None
    assert "running" in out.lower()
    assert "job completed" not in out.lower()


def test_extract_pending_media_screenshot_format():
    """Match the AI Agent confirm card wording from production screenshots."""
    history = [
        {
            "role": "assistant",
            "content": (
                "Here's the campaign plan:\n\n"
                "- **FROM:** Visakh\n"
                "- **TO:** Audience 12 (Leads Kerala) — 401 live recipients\n"
                "- **MEDIA:** UID 1000 (GATC RAM video, 30.1 MB)\n"
                "- **WINDOW:** 36 hours (paced background send with human-like random gaps)\n\n"
                "Please confirm if you want me to proceed."
            ),
        }
    ]
    pending = _extract_pending_media_campaign(history)
    assert pending is not None
    assert pending["uid"] == 1000
    assert pending["audience_uid"] == 12
    assert pending["total_hours"] == 36
    assert "Visakh" in pending["from_agent_name"]
