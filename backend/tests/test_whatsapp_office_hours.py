from datetime import datetime
from zoneinfo import ZoneInfo

from app.whatsapp_office_hours import split_office_off_seconds

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def _ist(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=IST)


def test_entirely_within_weekday_office():
    a = _ist(2026, 3, 2, 10, 0).astimezone(UTC)  # Monday
    b = _ist(2026, 3, 2, 10, 15).astimezone(UTC)
    total, office, off = split_office_off_seconds(a, b)
    assert total == 15 * 60
    assert office == 15 * 60
    assert off == 0


def test_span_office_close_to_next_morning():
    a = _ist(2026, 3, 6, 17, 0).astimezone(UTC)  # Friday 5pm
    b = _ist(2026, 3, 9, 10, 0).astimezone(UTC)  # Monday 10am
    total, office, off = split_office_off_seconds(a, b)
    assert total == int((b - a).total_seconds())
    assert office == 30 * 60 + 30 * 60  # Fri 17:00-17:30 + Mon 09:30-10:00
    assert office + off == total


def test_saturday_shorter_window():
    a = _ist(2026, 3, 7, 16, 30).astimezone(UTC)  # Saturday
    b = _ist(2026, 3, 7, 18, 0).astimezone(UTC)
    total, office, off = split_office_off_seconds(a, b)
    assert office == 30 * 60
    assert off == 60 * 60