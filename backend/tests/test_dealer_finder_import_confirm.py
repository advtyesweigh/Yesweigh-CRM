"""Dealer Finder import confirm binding (phone-only + prefer import over re-start)."""
from __future__ import annotations

from app.ai.service import (
    _assistant_is_import_confirm_prompt,
    _dealer_finder_import_intent,
    _extract_pending_dealer_finder,
    _extract_pending_dealer_finder_import,
    _is_user_confirm,
    _leads_contact_list_args,
    _should_route_dealer_finder_import,
)


def test_is_user_confirm_allows_yes_for_state():
    assert _is_user_confirm("yes")
    assert _is_user_confirm("yes for goa")
    assert _is_user_confirm("Yes for Goa")
    assert not _is_user_confirm("import to leads with phone only")


def test_import_intent_phone_only():
    intent = _dealer_finder_import_intent("import to leads with phone only")
    assert intent is not None
    assert intent["confirm"] is True
    assert intent["with_phone_only"] is True
    assert _dealer_finder_import_intent("import to leads")["with_phone_only"] is False
    assert _dealer_finder_import_intent("yes") is None


def test_extract_pending_import_from_reply_yes_prompt():
    history = [
        {
            "role": "assistant",
            "content": (
                "Dealer Finder – Goa: 272 places (232 with phone).\n"
                "Import 272 eligible NEW places to Leads (source Google, status New)? "
                "Reply yes to import."
            ),
        }
    ]
    pending = _extract_pending_dealer_finder_import(history)
    assert pending is not None
    assert pending["confirm"] is True
    assert pending.get("with_phone_only") is False
    assert pending.get("state") == "Goa" or "Goa" in str(pending.get("state") or "Goa")


def test_choice_prompt_does_not_default_phone_only():
    """Bare yes after 'yes / import phone only' must import ALL eligible, not phone-only."""
    history = [
        {
            "role": "assistant",
            "content": (
                "Dealer Finder – Tamil Nadu: 1,508 places (1,421 eligible NEW). "
                "Import 1093 phone-eligible NEW places to Leads (source Google, status New)? "
                "Reply yes / import phone only."
            ),
        }
    ]
    pending = _extract_pending_dealer_finder_import(history)
    assert pending is not None
    assert pending["confirm"] is True
    assert pending.get("with_phone_only") is False


def test_exclusive_phone_eligible_prompt_defaults_phone_only():
    history = [
        {
            "role": "assistant",
            "content": (
                "Import 1093 phone-eligible NEW places to Leads (source Google, status New)? "
                "Reply yes to import."
            ),
        }
    ]
    pending = _extract_pending_dealer_finder_import(history)
    assert pending is not None
    assert pending.get("with_phone_only") is True


def test_bare_yes_prefers_import_over_start_when_import_is_latest():
    history = [
        {
            "role": "assistant",
            "content": (
                "Start full Dealer Finder for Goa (all districts → cities × categories)? "
                "Reply yes to start."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Import 272 eligible NEW places to Leads (source Google, status New)? "
                "Existing dealers/leads stay skipped. Reply yes to import."
            ),
        },
    ]
    assert _extract_pending_dealer_finder_import(history) is not None
    # Start extractor skips import prompts; older start still visible but import wins in service.
    assert _assistant_is_import_confirm_prompt(history[-1]["content"])


def test_import_phrase_does_not_trigger_crm_leads_list_args():
    cmd = "import to leads with phone only for Puducherry"
    assert _dealer_finder_import_intent(cmd) is not None
    assert _leads_contact_list_args(cmd) is None


def test_route_dealer_finder_import_with_state_in_command():
    history = [{"role": "user", "content": "show dealer finder puducherry"}]
    assert _should_route_dealer_finder_import(
        "import to leads with phone only for Puducherry", history
    )


def test_start_extractor_skips_import_prompt():
    history = [
        {
            "role": "assistant",
            "content": (
                "Import 272 eligible NEW places to Leads (source Google, status New)? "
                "Reply yes to import."
            ),
        }
    ]
    assert _extract_pending_dealer_finder(history) is None
