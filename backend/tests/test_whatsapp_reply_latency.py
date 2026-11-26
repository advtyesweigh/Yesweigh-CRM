from datetime import datetime, timedelta, timezone

from app.models import WhatsAppConversation
from app.whatsapp_reply_latency import format_reply_duration, short_staff_name, sync_awaiting_since
from app.whatsapp_reply_needed import mark_staff_replied


def test_short_staff_name():
    assert short_staff_name("YW Biju B") == "Biju"
    assert short_staff_name("Namrata Kumari") == "Namrata"


def test_sync_awaiting_since_sets_once():
    conv = WhatsAppConversation(customer_jid="9199@s.whatsapp.net")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    sync_awaiting_since(conv, needs_reply=True, at=t0)
    assert conv.awaiting_since == t0
    t1 = t0 + timedelta(hours=1)
    sync_awaiting_since(conv, needs_reply=True, at=t1)
    assert conv.awaiting_since == t0


def test_mark_staff_replied_clears_awaiting_since():
    conv = WhatsAppConversation(
        customer_jid="9199@s.whatsapp.net",
        awaiting_staff_reply=True,
        awaiting_since=datetime.now(timezone.utc),
    )
    mark_staff_replied(conv)
    assert conv.awaiting_staff_reply is False
    assert conv.awaiting_since is None


def test_format_reply_duration():
    assert format_reply_duration(45) == "1m"
    assert format_reply_duration(3900) == "1h 5m"