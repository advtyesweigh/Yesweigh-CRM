"""Routing: google + dealers + state → start_dealer_finder, never CRM dump."""
from __future__ import annotations

from app.ai.service import (
    _dealer_contact_list_args,
    _google_statewide_dealer_finder_args,
    _mentions_google_discovery,
)


def test_canonical_tamilnadu_from_google():
    cmd = "weighing scale dealers list Tamilnadu from google"
    assert _mentions_google_discovery(cmd)
    finder = _google_statewide_dealer_finder_args(cmd)
    assert finder is not None
    assert finder["state"] == "Tamil Nadu"
    assert finder["confirm"] is False
    assert _dealer_contact_list_args(cmd) is None


def test_find_pattern_from_google():
    cmd = "find weighing scale dealer tamilnadu from google"
    finder = _google_statewide_dealer_finder_args(cmd)
    assert finder is not None
    assert finder["state"] == "Tamil Nadu"
    assert _dealer_contact_list_args(cmd) is None


def test_crm_dealer_list_without_google_still_works():
    cmd = "all Kerala dealers WhatsApp numbers"
    assert _google_statewide_dealer_finder_args(cmd) is None
    contact = _dealer_contact_list_args(cmd)
    assert contact is not None
    assert contact["dataset"] == "dealers"
    assert "Kerala" in str(contact["state"])


def test_dealers_list_state_without_google():
    cmd = "dealers list Tamil Nadu"
    assert _google_statewide_dealer_finder_args(cmd) is None
    contact = _dealer_contact_list_args(cmd)
    assert contact is not None
    assert contact["dataset"] == "dealers"


def test_pondicherry_state_equals_from_google():
    cmd = "serch dealers from google state = pondichery"
    assert _mentions_google_discovery(cmd)
    finder = _google_statewide_dealer_finder_args(cmd)
    assert finder is not None
    assert finder["state"] == "Puducherry"
    assert _dealer_contact_list_args(cmd) is None


def test_pondicherry_context_clarification_weighing_only():
    cmd = "dealer = weighing scale related only"
    ctx = "serch dealers from google state = pondichery"
    finder = _google_statewide_dealer_finder_args(cmd, context=ctx)
    assert finder is not None
    assert finder["state"] == "Puducherry"


def test_google_dealers_without_weighing_words_still_finder():
    cmd = "search dealers from google state = pondichery"
    finder = _google_statewide_dealer_finder_args(cmd)
    assert finder is not None
    assert finder["state"] == "Puducherry"
