"""Unit tests for AI WhatsApp send safeguards (no DB)."""
from __future__ import annotations

from types import SimpleNamespace

from app.ai.whatsapp_send_guards import (
    blocked_recipient_error,
    classify_blocked_recipient,
    english_core_text,
    filter_recipients_excluding_blocked,
    filter_recipients_excluding_prior_campaign_media,
    filter_recipients_excluding_spam,
    looks_like_container_strike_notice,
    normalize_outbound_text,
    phone_key,
    texts_are_duplicate,
)


def test_phone_key_normalizes_in_variants():
    assert phone_key("+91 85906 01638") == "8590601638"
    assert phone_key("918590601638") == "8590601638"
    assert phone_key("08590601638") == "8590601638"


def test_classify_self_and_staff():
    self_keys = {"8590601638"}
    staff_keys = {"8590601638", "9876543210"}
    assert classify_blocked_recipient("918590601638", self_keys=self_keys, staff_keys=staff_keys) == "self"
    assert classify_blocked_recipient("9876543210", self_keys=self_keys, staff_keys=staff_keys) == "staff"
    assert classify_blocked_recipient("9123456789", self_keys=self_keys, staff_keys=staff_keys) is None


def test_filter_strips_prior_campaign_media_dupes():
    recipients = [
        {"to_phone": "9123456789", "customer_name": "Dealer A"},
        {"to_phone": "+91 98765 43210", "customer_name": "Dealer B"},
        {"to_phone": "9988776655", "customer_name": "Dealer C"},
    ]
    allowed, excluded = filter_recipients_excluding_prior_campaign_media(
        recipients, prior_phone_keys={"9123456789", "9876543210"}
    )
    assert [r["customer_name"] for r in allowed] == ["Dealer C"]
    assert excluded[0]["reason"] == "duplicate_prior_media"


def test_filter_strips_spam_phones_by_normalized_key():
    recipients = [
        {"to_phone": "919876543210", "customer_name": "Spam A"},
        {"to_phone": "+91 99887 76655", "customer_name": "Ok"},
        {"to_phone": "9876543210", "customer_name": "Spam B again"},
    ]
    allowed, excluded = filter_recipients_excluding_spam(
        recipients, spam_phone_keys={"9876543210"}
    )
    assert [r["customer_name"] for r in allowed] == ["Ok"]
    assert all(e["reason"] == "spam" for e in excluded)
    assert len(excluded) == 2


def test_filter_strips_self_staff_and_batch_dupes():
    self_keys = {"8590601638"}
    staff_keys = {"8590601638", "9000000001"}
    recipients = [
        {"to_phone": "9123456789", "customer_name": "Dealer A"},
        {"to_phone": "+91 85906 01638", "customer_name": "Biju"},
        {"to_phone": "9000000001", "customer_name": "Staff"},
        {"to_phone": "9123456789", "customer_name": "Dealer A again"},
        {"to_phone": "9988776655", "customer_name": "Dealer B"},
    ]
    allowed, excluded = filter_recipients_excluding_blocked(
        recipients, self_keys=self_keys, staff_keys=staff_keys
    )
    assert [r["customer_name"] for r in allowed] == ["Dealer A", "Dealer B"]
    reasons = {e["reason"] for e in excluded}
    assert "self" in reasons
    assert "staff" in reasons
    assert "duplicate_in_batch" in reasons


def test_blocked_error_mentions_from_not_to():
    inst = SimpleNamespace(
        employee_name="Biju B",
        display_name="YW Biju B",
        evolution_instance_name="biju",
    )
    err = blocked_recipient_error("self", "8590601638", inst)
    assert "VIA" in err or "from Biju" in err.lower() or "not TO" in err
    assert "8590601638" in err


def test_normalize_strips_whitespace_and_punct():
    assert normalize_outbound_text("  Hello,  World!! ") == "hello world"


def test_english_core_ignores_malayalam_and_signature():
    msg = (
        "Dear Customer, the container strike is over, and we will resume dispatching goods on Tuesday.\n"
        "പ്രിയ ഉപഭോക്താവ്, കണ്ടെയ്നർ സമരം അവസാനിച്ചു.\n"
        "- YesWeigh"
    )
    core = english_core_text(msg)
    assert "container strike" in core
    assert "tuesday" in core
    assert "yesweigh" not in core
    assert "പ്രിയ" not in core


def test_reworded_strike_notices_are_duplicates():
    a = (
        "Dear Customer, the container strike is over, and we will resume dispatching goods on Tuesday.\n"
        "പ്രിയ ഉപഭോക്താവ്, കണ്ടെയ്നർ സമരം അവസാനിച്ചു."
    )
    b = (
        "Dear Customer, the container strike is over. We will resume dispatching goods from Tuesday. - YesWeigh\n"
        "പ്രിയ ഉപഭോക്താവ്, ചരക്ക് അയക്കൽ ചൊവ്വാഴ്ച മുതൽ പുനരാരംഭിക്കും."
    )
    assert texts_are_duplicate(a, b)
    assert looks_like_container_strike_notice(a)
    assert looks_like_container_strike_notice(b)


def test_unrelated_messages_not_duplicates():
    assert not texts_are_duplicate(
        "Your order is ready for pickup today.",
        "Dear Customer, the container strike is over, and we will resume dispatching goods on Tuesday.",
    )
