"""Observe-phase intelligence: rules, flags, funnel shape. No Gemini and no reply-flag writes."""
from datetime import date
from types import SimpleNamespace

from pydantic import ValidationError

from app.ai.budget import purpose_bucket
from app.intel.extract import (
    SERVICE_OBSERVE_POLICY,
    apply_secondary_intents,
    conversation_is_intel_subject,
    detect_secondary_intents,
    enqueue_decision,
    extract_commitment_from_message,
    idempotency_key,
    next_commitment_status,
    service_keyword_tags,
    should_insert_job,
)
from app.intel.flags import intel_access, intel_enabled_value
from app.intel.health import customer_ids_for_health, health_band, india_fy_bounds
from app.intel.narrate import numbers_guard
from app.intel.queries import empty_funnel_shell, require_sources, sum_open_invoice_balances
from app.intel.router import ServiceThreadOut
from app.sales_amount import is_countable_invoice


def test_flag_defaults_off():
    assert intel_enabled_value(None) is False
    assert intel_enabled_value(SimpleNamespace(intel_enabled=False)) is False
    assert intel_enabled_value(SimpleNamespace(intel_enabled=True)) is True


def test_group_newsletter_broadcast_not_enqueued():
    assert enqueue_decision(enabled=True, jid="120363@g.us") is False
    assert enqueue_decision(enabled=True, jid="123@newsletter") is False
    assert enqueue_decision(enabled=True, jid="status@broadcast") is False
    assert conversation_is_intel_subject("919812345678@s.whatsapp.net") is True
    assert enqueue_decision(enabled=False, jid="919812345678@s.whatsapp.net") is False


def test_commitment_does_not_carry_reply_flag():
    hit = extract_commitment_from_message(
        body="We will send the quote by Friday",
        direction="out",
        today=date(2026, 11, 24),
        message_id=9,
        conversation_id=3,
    )
    assert hit is not None
    assert "awaiting_staff_reply" not in hit
    assert hit["owner_side"] == "staff"
    assert hit["status"] == "open"
    assert hit["confidence"] == "high"
    assert hit["due_on"] == date(2026, 11, 27)
    assert hit["extractor"] == "rule"


def test_commitment_without_due_date_stays_open():
    hit = extract_commitment_from_message(
        body="I will call you",
        direction="in",
        today=date(2026, 11, 24),
        message_id=1,
        conversation_id=2,
    )
    assert hit is not None
    assert hit["due_on"] is None
    assert hit["confidence"] == "medium"
    assert next_commitment_status("open", None, date(2026, 11, 24)) == "open"
    conv = SimpleNamespace(awaiting_staff_reply=True)
    status = next_commitment_status("open", date(2026, 11, 1), date(2026, 11, 24))
    assert status == "missed"
    assert conv.awaiting_staff_reply is True


def test_staff_check_and_two_minute_promises():
    hit = extract_commitment_from_message(
        body="2 min sir",
        transcript="രണ്ട് മിനിറ്റ്",
        direction="out",
        today=date(2026, 11, 24),
        message_id=5185,
        conversation_id=278,
    )
    assert hit is not None
    assert hit["owner_side"] == "staff"
    hit2 = extract_commitment_from_message(
        body="",
        transcript="സാറേ നോക്കുന്നുണ്ട് അഞ്ചുപത്ത് മിനിറ്റ് ഇപ്പോൾ അയച്ചുതരാം",
        direction="out",
        today=date(2026, 11, 24),
        message_id=5256,
        conversation_id=278,
    )
    assert hit2 is not None
    assert hit2["owner_side"] == "staff"


def test_container_strike_blast_is_not_a_commitment():
    hit = extract_commitment_from_message(
        body=(
            "Dear Customer, the container strike is over. "
            "We will resume dispatching goods from Tuesday. - YesWeigh"
        ),
        direction="out",
        today=date(2026, 11, 24),
        message_id=9001,
        conversation_id=278,
    )
    assert hit is None


def test_bare_malayalam_look_voice_is_not_a_commitment():
    hit = extract_commitment_from_message(
        body="Voice message കണ്ടുനോക്കൂ.",
        direction="out",
        today=date(2026, 11, 24),
        message_id=9002,
        conversation_id=278,
    )
    assert hit is None


def test_low_confidence_off_high_lane():
    hit = extract_commitment_from_message(
        body="We will send the price tomorrow",
        direction="note",
        today=date(2026, 11, 24),
        message_id=4,
        conversation_id=8,
    )
    assert hit is not None
    assert hit["confidence"] == "low"
    assert hit["high_lane"] is False


def test_finding_without_source_rejected():
    try:
        require_sources([])
        raised = False
    except ValueError:
        raised = True
    assert raised
    try:
        require_sources([{"type": "", "id": "1"}])
        raised = False
    except ValueError:
        raised = True
    assert raised
    sources = require_sources([{"type": "dealer", "id": "15", "label": "ELMASS", "href": "/dealers?q=ELMASS"}])
    assert sources[0]["type"] == "dealer"


def test_idempotent_enqueue_key():
    key = idempotency_key("rebuild_findings", "organization", "yesweigh", "2026-11-24")
    assert key == idempotency_key("rebuild_findings", "organization", "yesweigh", "2026-11-24")
    assert should_insert_job(existing_key=key, new_key=key) is False
    assert should_insert_job(existing_key=None, new_key=key) is True


def test_funnel_has_no_dispatch_stage():
    payload = empty_funnel_shell()
    blob = str(payload).lower()
    assert "dispatch" not in blob
    keys = [stage["key"] for stage in payload["stages"]]
    assert keys == [
        "enquiry",
        "quotation_language",
        "sales_order",
        "invoice_open",
        "invoice_settled",
    ]
    assert "paid_awaiting_dispatch" not in keys


def test_open_balance_skips_void_invoice():
    total = sum_open_invoice_balances(
        [
            {"document_type": "invoice", "status": "sent", "balance": "1500"},
            {"document_type": "invoice", "status": "void", "balance": "9000"},
            {"document_type": "invoice", "status": "cancelled", "balance": "20"},
            {"document_type": "invoice", "status": "draft", "balance": "5"},
            {"document_type": "salesorder", "status": "open", "balance": "400"},
        ]
    )
    assert total == 1500
    assert is_countable_invoice("invoice", "void") is False
    assert is_countable_invoice("invoice", "paid") is True


def test_numbers_guard_drops_extra_numeral():
    assert numbers_guard("Open balance is 1200.", '{"balance": 1200}') == "Open balance is 1200."
    assert numbers_guard("About 99 dealers need a call", '{"balance": 1200}') is None


def test_service_schema_has_no_procedure():
    assert "advice" not in ServiceThreadOut.model_fields
    assert "procedure" not in ServiceThreadOut.model_fields
    ServiceThreadOut(conversation_id="abc", tags=["repair"])
    try:
        ServiceThreadOut(conversation_id="abc", advice="how to calibrate")  # type: ignore[call-arg]
        rejected = False
    except ValidationError:
        rejected = True
    assert rejected
    policy = SERVICE_OBSERVE_POLICY.lower()
    assert "calibration" in policy
    assert "firmware" in policy
    assert "metrology" in policy
    assert "forbidden" in policy
    tags = service_keyword_tags("please calibrate the scale and repair the load cell")
    assert "calibration" in tags
    assert "repair" in tags
    assert all(" " not in tag for tag in tags)


def test_secondary_intents_do_not_replace_chip():
    conv = SimpleNamespace(service_category="sales")
    primary, secondary = apply_secondary_intents(conv, ["price", "repair", "service", "dispatch_chatter"])
    assert conv.service_category == "sales"
    assert primary == "sales"
    assert secondary == ["price", "repair", "dispatch_chatter"]
    assert "dispatch_chatter" in detect_secondary_intents("please dispatch tomorrow")


def test_health_band_and_merge_ids():
    assert health_band(active=True, open_service_threads=0, unreplied_threads=0, open_commitments=0) == "steady"
    assert health_band(active=False, open_service_threads=0, unreplied_threads=0, open_commitments=0) == "quiet"
    assert health_band(active=True, open_service_threads=0, unreplied_threads=1, open_commitments=0) == "attention"
    elmass = customer_ids_for_health("99381000005074017")
    assert "99381000000084733" in elmass
    venkat = customer_ids_for_health("99381000014693003")
    assert "99381000000747828" in venkat
    start, today, prior_start, prior_end = india_fy_bounds(date(2026, 11, 24))
    assert (start, today, prior_start, prior_end) == ("2026-04-01", "2026-11-24", "2025-04-01", "2026-03-31")


def test_intel_budget_bucket_is_separate_from_outline():
    assert purpose_bucket("whatsapp_outline") == "whatsapp"
    assert purpose_bucket("command") == "agent"
    assert purpose_bucket("intel_narrate") == "intel"
    assert purpose_bucket("intel_ask") == "intel"


def test_service_manager_sees_service_only():
    assert intel_access(SimpleNamespace(role="service_manager")) == "service"
    assert intel_access(SimpleNamespace(role="sales_manager")) == "full"
    assert intel_access(SimpleNamespace(role="management")) == "full"
    assert intel_access(SimpleNamespace(role="campaigns_only")) is None
