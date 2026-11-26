from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from app.whatsapp_service_meta import (
    format_hold_clock,
    heuristic_service_category,
    service_category_from_summary,
)


def test_heuristic_service_keywords():
    assert heuristic_service_category("factory reset and service charge 500") == "service"
    assert heuristic_service_category("wifi connection issue on scale") == "service"
    assert heuristic_service_category("ecs dust cover stock") == "service"


def test_heuristic_sales_keywords():
    assert heuristic_service_category("price for new weighing machine quotation") == "sales"
    assert heuristic_service_category("dealer price and retail price in Kochi specs") == "sales"
    assert heuristic_service_category("SALES ORDER 4 nos courier to Tirur order confirmed") == "sales"


def test_heuristic_stamping_keywords():
    assert heuristic_service_category("Yo 2428 50 kg ov send certificate") == "stamping"
    assert heuristic_service_category("Reached camp inspector called please send OV document") == "stamping"
    assert heuristic_service_category("സ്റ്റാമ്പ് ഓവി രേഖ അയച്ചു തരാമോ") == "stamping"
    assert heuristic_service_category("need stamping plate for new machine") == "sales"
    # Sandhya-style OV serial + Malayalam send + Legal Metrology certificate
    assert heuristic_service_category("Y01277,Y01278 ov\nUrgent anu\nOv send cheyth") == "stamping"
    assert (
        heuristic_service_category(
            "CERTIFICATE OF VERIFICATION\nI hereby certify that I have this day verified and stamped"
        )
        == "stamping"
    )
    assert (
        heuristic_service_category(
            "സാറേ, വാർക്കോട് മെഷീന്റെ സീൽ ചെയ്ത പേപ്പർ ഇതിന്റെ കൂടെ വെച്ചിട്ട് കാണുന്നില്ലല്ലോ"
        )
        == "stamping"
    )
    assert heuristic_service_category("please send sealed paper https://yesgatc.in/download") == "stamping"


def test_heuristic_cloud_keywords():
    assert (
        heuristic_service_category(
            "Please pay ₹2242 per scale to renew Ezee Mart 2 Unlimited Sales Logs"
        )
        == "cloud"
    )
    assert heuristic_service_category("Weighing Sync subscription renewal required") == "cloud"
    assert heuristic_service_category("cloud recharge standard subscription ended") == "cloud"
    assert heuristic_service_category("Hey YesWeigh, Renew Subscription, ShopId=3967") == "cloud"
    assert heuristic_service_category("ECS Lite Weighing Sync Standard MSS subscription") == "cloud"
    assert heuristic_service_category("Sales Sync Update Subscription Validity 2027-11-01") == "cloud"
    assert (
        heuristic_service_category("ലൈസൻസ് പുതുക്കൽ സോഫ്റ്റ്‌വെയർ സബ്സ്ക്രിപ്ഷൻ പുതുക്കുക ഷോപ്പ് ഐഡി 1785")
        == "cloud"
    )


def test_service_from_ai_summary_field():
    summary = {"service_category": "support", "topic": "repair visit"}
    assert service_category_from_summary(summary) == "sales"

    summary_sales = {"service_category": "sales", "topic": "bench scale quote"}
    assert service_category_from_summary(summary_sales) == "sales"

    summary_cloud = {
        "service_category": "service",
        "conversation_status": "Renewal Required",
        "topic": "subscription renew",
    }
    assert service_category_from_summary(summary_cloud) == "cloud"

    summary_shop = {
        "service_category": "service",
        "conversation_status": "Renewal Required",
        "product_interest": ["Software"],
        "customer_intent": "Shop subscription renew",
        "important_information": ["Shop ID: 3967"],
    }
    assert service_category_from_summary(
        summary_shop, fallback_text="Renew Subscription ShopId=3967"
    ) == "cloud"

    summary_golden = {
        "service_category": "sales",
        "conversation_status": "Payment Pending",
        "topic": "ലൈസൻസ് പുതുക്കൽ",
        "customer_intent": "സോഫ്റ്റ്‌വെയർ സബ്സ്ക്രിപ്ഷൻ പുതുക്കുക",
        "current_issue": "പണം അടച്ചു, തുടർനടപടികൾ ആവശ്യമാണ്",
        "product_interest": ["Standard MSS"],
        "important_information": ["ഷോപ്പ് ഐഡി 1785", "പണം അടച്ചതായി അറിയിച്ചു"],
    }
    assert (
        service_category_from_summary(
            summary_golden,
            fallback_text="Hey YesWeigh, Renew Subscription, ShopId=1785 Weighing Sync",
        )
        == "cloud"
    )


def test_outline_fingerprint_fits_db_column():
    from app.whatsapp_outline import outline_fingerprint

    msg = SimpleNamespace(
        public_id="550e8400-e29b-41d4-a716-446655440000",
        created_at=datetime(2026, 11, 24, tzinfo=timezone.utc),
    )
    fp = outline_fingerprint([msg] * 120, preferred_language="ml")
    assert len(fp) <= 128
    assert "550e8400" not in fp


def test_format_hold_clock():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=14, minutes=30)
    iso, label = format_hold_clock(since, now=now)
    assert iso
    assert label == "14h 30m"