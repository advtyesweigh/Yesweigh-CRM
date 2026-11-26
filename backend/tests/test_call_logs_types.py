from datetime import datetime, timedelta, timezone

from app.call_logs import CALL_TYPES, ONLINE_THRESHOLD, _direction_for, _is_online, _norm_phone


def test_call_types_set():
    assert CALL_TYPES == frozenset({"missed", "received", "rejected", "outbound"})


def test_direction_for():
    assert _direction_for("outbound", "") == "outbound"
    assert _direction_for("missed", "") == "inbound"
    assert _direction_for("received", "outbound") == "outbound"


def test_norm_phone_indian_mobile():
    assert _norm_phone("9876543210").endswith("9876543210")
    assert len(_norm_phone("+91 98765 43210")) >= 10


def test_is_online_threshold():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    assert _is_online(None, now=now) is False
    assert _is_online(now - timedelta(seconds=60), now=now) is True
    assert _is_online(now - ONLINE_THRESHOLD, now=now) is True
    assert _is_online(now - ONLINE_THRESHOLD - timedelta(seconds=1), now=now) is False
    assert ONLINE_THRESHOLD.total_seconds() == 150
