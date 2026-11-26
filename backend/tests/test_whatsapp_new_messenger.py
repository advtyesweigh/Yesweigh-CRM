"""Unit tests for new-messenger role parsing and instance gating."""
from __future__ import annotations

from types import SimpleNamespace

from app.whatsapp_new_messenger import (
    MSG_ASK_CARD,
    MSG_ASK_ROLE,
    included_instances_doc,
    instance_included,
    parse_role_reply,
)


def test_parse_role_dealer_and_customer():
    assert parse_role_reply("Dealer") == "dealer"
    assert parse_role_reply("I am a dealer") == "dealer"
    assert parse_role_reply("customer") == "customer"
    assert parse_role_reply("not a dealer") == "customer"
    assert parse_role_reply("ഡീലർ") == "dealer"
    assert parse_role_reply("hello price list?") is None
    assert parse_role_reply("📷 Photo") is None


def test_message_copy_mentions_dealer_and_customer():
    assert "dealer" in MSG_ASK_ROLE.lower()
    assert "customer" in MSG_ASK_ROLE.lower()
    assert "business card" in MSG_ASK_CARD.lower()


def test_instance_included_evolution_and_care():
    assert instance_included(SimpleNamespace(provider="evolution", deleted_at=None))
    assert instance_included(SimpleNamespace(provider="meta_cloud", deleted_at=None))
    assert instance_included(SimpleNamespace(provider="", deleted_at=None))  # default evolution
    assert not instance_included(SimpleNamespace(provider="other", deleted_at=None))
    assert not instance_included(None)
    doc = included_instances_doc()
    assert "evolution" in doc["providers"]
    assert "meta_cloud" in doc["providers"]
