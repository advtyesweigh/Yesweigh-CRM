"""Routing: create audience from leads must never trigger Dealer Finder."""
from __future__ import annotations

from app.ai.service import (
    _audience_create_args,
    _audience_create_intent_from_history,
    _dealer_finder_user_blocked,
    _google_statewide_dealer_finder_args,
    _latest_assistant_confirm_kind,
    _leads_contact_list_args,
    _should_route_dealer_finder_import,
)


def test_canonical_tn_py_lead_audience_command():
    cmd = "create audience TN PY lead from leads state = Tamil Nadu|Puducherry"
    args = _audience_create_args(cmd)
    assert args is not None
    assert args["name"] == "TN PY lead"
    assert args["source"] == "leads"
    assert args["state"] == "Tamil Nadu|Puducherry"
    assert _dealer_finder_user_blocked(cmd)
    assert _google_statewide_dealer_finder_args(cmd) is None
    assert _should_route_dealer_finder_import(cmd, []) is False
    assert _leads_contact_list_args(cmd) is None


def test_from_leads_blocks_finder_even_with_tamil_nadu():
    cmd = "not from dealer from leads leads"
    assert _dealer_finder_user_blocked(cmd)
    ctx = "Start full Dealer Finder for Tamil Nadu"
    assert _dealer_finder_user_blocked(cmd, context=ctx)


def test_bare_yes_blocks_dealer_finder_when_thread_was_audience_create():
    history = [
        {
            "role": "user",
            "content": (
                "create audience TN PY lead from leads state = Tamil Nadu|Puducherry"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Start full Dealer Finder for **Tamil Nadu** — reply yes to start"
            ),
        },
    ]
    assert _dealer_finder_user_blocked("yes", context=history[0]["content"])
    assert _latest_assistant_confirm_kind(history) == "dealer_finder"
    recovered = _audience_create_intent_from_history(history, command="yes")
    assert recovered is not None
    assert recovered.get("source") == "leads"