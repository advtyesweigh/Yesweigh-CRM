"""Indian phone mobile vs landline classification."""
from __future__ import annotations

from app.phone_util import classify_indian_phone, is_indian_mobile


def test_mobile_patterns():
    assert classify_indian_phone("7592052528") == "mobile"
    assert classify_indian_phone("+91 75920 52528") == "mobile"
    assert classify_indian_phone("917592052528") == "mobile"
    assert classify_indian_phone("07592052528") == "mobile"
    assert is_indian_mobile("9095060515")


def test_landline_std():
    assert classify_indian_phone("04841234567") == "landline"
    assert classify_indian_phone("4841234567") == "landline"
    assert classify_indian_phone("01123456789") == "landline"
    assert not is_indian_mobile("04841234567")


def test_empty_and_short():
    assert classify_indian_phone("") == "empty"
    assert classify_indian_phone("12345") == "invalid"
    assert not is_indian_mobile("")