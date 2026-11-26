"""Phone → Evolution number formatting and batch fatal-error classification."""
from __future__ import annotations

from app.ai.tools import (
    _is_fatal_batch_send_error,
    _is_recoverable_instance_disconnect_error,
    _is_recoverable_paced_sender_outage,
)
from app.evolution_service import is_transient_media_upload_error
from app.phone_util import (
    normalize_in_mobile,
    repair_stored_in_mobile,
    repair_stored_whatsapp_jid,
    store_crm_phone,
    to_evolution_number,
    whatsapp_pn_jid,
)


def test_to_evolution_number_adds_in_country_code():
    assert to_evolution_number("7592052528") == "917592052528"
    assert to_evolution_number("+91 75920 52528") == "917592052528"
    assert to_evolution_number("917592052528") == "917592052528"
    assert to_evolution_number("07592052528") == "917592052528"


def test_to_evolution_number_preserves_jid():
    assert to_evolution_number("12345@lid") == "12345@lid"
    assert to_evolution_number("917592052528@s.whatsapp.net") == "917592052528@s.whatsapp.net"


def test_whatsapp_pn_jid():
    assert whatsapp_pn_jid("7592052528") == "917592052528@s.whatsapp.net"
    assert normalize_in_mobile("917592052528") == "7592052528"


def test_store_crm_phone():
    assert store_crm_phone("8322313219") == "918322313219"
    assert store_crm_phone("918322313219") == "918322313219"


def test_repair_stored_in_mobile_no_double_prefix():
    assert repair_stored_in_mobile("7592052528") == ("917592052528", True)
    assert repair_stored_in_mobile("917592052528") == ("917592052528", False)
    assert repair_stored_in_mobile("+91 75920 52528") == ("917592052528", True)
    assert repair_stored_in_mobile("07592052528") == ("917592052528", True)
    assert repair_stored_in_mobile("12345") == ("12345", False)
    assert repair_stored_in_mobile("12345@lid") == ("12345@lid", False)
    assert repair_stored_in_mobile("") == ("", False)


def test_repair_stored_whatsapp_jid():
    assert repair_stored_whatsapp_jid("7592052528@s.whatsapp.net") == (
        "917592052528@s.whatsapp.net",
        True,
    )
    assert repair_stored_whatsapp_jid(
        "7592052528@s.whatsapp.net", phone="917592052528"
    ) == ("917592052528@s.whatsapp.net", True)
    assert repair_stored_whatsapp_jid("12345@lid") == ("12345@lid", False)


def test_batch_continues_on_not_registered():
    err = (
        "Send failed: 502: Send failed: number +7592052528@s.whatsapp.net "
        "is not registered on WhatsApp"
    )
    assert _is_fatal_batch_send_error(err) is False


def test_batch_continues_on_generic_send_failed_and_timeout():
    assert _is_fatal_batch_send_error("Send failed: Media send failed") is False
    assert _is_fatal_batch_send_error("Send failed: timed out") is False
    assert _is_fatal_batch_send_error("Read timeout") is False


def test_batch_continues_on_wa_cdn_503_upload():
    err = "Send failed: 502: Send failed: upload failed with status code 503"
    assert _is_fatal_batch_send_error(err) is False
    assert is_transient_media_upload_error(err) is True
    assert is_transient_media_upload_error("boom", status_code=503) is True
    assert is_transient_media_upload_error("number not registered") is False


def test_batch_recoverable_disconnect_not_fatal():
    assert _is_recoverable_instance_disconnect_error("Send failed: instance is disconnected") is True
    assert _is_recoverable_instance_disconnect_error("Instance disconnected") is True
    assert _is_recoverable_instance_disconnect_error(
        "Instance is Disconnected — connect in Settings → WhatsApp"
    ) is True
    assert _is_recoverable_paced_sender_outage("connection refused") is True
    assert _is_fatal_batch_send_error("Send failed: instance is disconnected") is False
    assert _is_fatal_batch_send_error("Instance disconnected") is False
    assert _is_fatal_batch_send_error("connection refused") is False


def test_batch_stops_on_permanent_instance_errors():
    assert _is_fatal_batch_send_error("from instance not found") is True
    assert _is_fatal_batch_send_error("instance is logout") is True
    assert _is_fatal_batch_send_error("logged out") is True
