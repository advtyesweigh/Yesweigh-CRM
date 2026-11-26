from app.whatsapp_dealer_enquiry import (
    dealer_enquiry_text_signal,
    enrich_dealer_enquiry_recommended_action,
    is_dealer_enquiry_summary,
)
from app.whatsapp_service_meta import heuristic_service_category


def test_dealer_enquiry_keywords():
    assert dealer_enquiry_text_signal("May I know dealer prices for platform scale")
    assert dealer_enquiry_text_signal("wholesale price for weighing machines")
    assert dealer_enquiry_text_signal("I want to become a dealer in Nellore")
    assert not dealer_enquiry_text_signal("factory reset service charge")


def test_dealer_enquiry_sales_category():
    assert heuristic_service_category("dealer price for platform weighing scale models") == "sales"


def test_summary_subtype_detection():
    summary = {"enquiry_subtype": "dealer enquiry", "service_category": "sales"}
    assert is_dealer_enquiry_summary(summary)
    assert is_dealer_enquiry_summary(
        {"topic": "retail price only", "service_category": "sales"},
        fallback_text="",
    ) is False


def test_unattended_recommended_action():
    summary = {
        "language": "en",
        "current_issue": "Nobody called me",
        "conversation_status": "Awaiting Reply",
        "recommended_action": "",
        "important_information": [],
    }
    enrich_dealer_enquiry_recommended_action(summary, attended=False)
    assert "Biju" in summary["recommended_action"]
    assert summary["followup_priority"] == "High"