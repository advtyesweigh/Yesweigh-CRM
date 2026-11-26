"""Enquiry tab gate: sales/price only. No Gemini and no reply-flag writes."""
from app.ai.whatsapp_send_guards import phone_key
from app.intel.enquiry import (
    assess_enquiry,
    assess_enquiry_outline,
    cache_from_json,
    enquiry_fingerprint,
    should_rewrite_outline,
)

BULLSNET = (
    "Thank you for contacting BULLSNET! Please let us know how we can help you. "
    "All Sundays are Open. Daily @ 8 am to 9 pm"
)


def _msg(direction: str, text: str) -> dict[str, str]:
    return {"direction": direction, "text": text}


def test_business_auto_reply_is_not_an_enquiry():
    verdict = assess_enquiry(
        service_category="sales",
        outline={"conversation_status": "New Enquiry", "service_category": "sales", "topic": "New dealer"},
        messages=[_msg("out", "Sharing the product video"), _msg("in", BULLSNET)],
        customer_name="BULLSNET THRISSUR",
    )
    assert verdict.include is False
    assert verdict.lane == "auto_reply"
    assert verdict.outline_patch is not None
    assert verdict.outline_patch["conversation_status"] == ""
    assert verdict.outline_patch["service_category"] == ""
    assert should_rewrite_outline(
        {"conversation_status": "New Enquiry", "service_category": "sales", "topic": "New dealer"},
        verdict,
    )


def test_service_fault_is_not_an_enquiry():
    verdict = assess_enquiry(
        service_category="service",
        outline={"conversation_status": "Service Required", "service_category": "service"},
        messages=[_msg("in", "Machine fault, urgent repair at Kalamassery and Palakkad")],
        customer_name="Shahaban",
    )
    assert verdict.include is False
    assert verdict.lane == "service"
    assert verdict.outline_patch is None


def test_repair_mislabeled_as_new_enquiry_is_service():
    verdict = assess_enquiry(
        service_category="sales",
        outline={"conversation_status": "New Enquiry", "service_category": "sales"},
        messages=[_msg("in", "Urgent repair, machine not working")],
        customer_name="Shahaban",
    )
    assert verdict.include is False
    assert verdict.lane == "service"
    assert verdict.outline_patch is not None
    assert verdict.outline_patch["conversation_status"] == "Service Required"


def test_technician_porter_chat_is_internal():
    verdict = assess_enquiry(
        service_category="sales",
        outline={"conversation_status": "New Enquiry", "service_category": "sales"},
        messages=[_msg("in", "Send the orange machine via Porter, Alleppey parcel")],
        customer_name="Sivaprasad Unnippan Vinayaka Technician",
    )
    assert verdict.include is False
    assert verdict.lane == "internal"


def test_staff_first_name_meeting_is_internal_but_price_ask_is_kept():
    meeting = assess_enquiry(
        service_category="sales",
        outline={"conversation_status": "New Enquiry", "service_category": "sales", "topic": "Meeting follow up"},
        messages=[_msg("in", "Please attend the meeting and make an urgent call to contact the customer")],
        customer_name="BIJU",
        staff_first={"biju", "visakh"},
    )
    assert meeting.include is False
    assert meeting.lane == "internal"

    price = assess_enquiry(
        service_category="sales",
        outline={"conversation_status": "New Enquiry", "service_category": "sales"},
        messages=[
            _msg("in", "Price of ECC5 and MSB"),
            _msg("out", "ECC5 is Rs 12500"),
        ],
        customer_name="BIJU",
        customer_phone="919800000000",
        staff_first={"biju", "visakh"},
    )
    assert price.include is True
    assert price.lane == "sales_price"
    assert price.unanswered == ("MSB",)
    assert "MSB" in price.summary
    assert "ECC5" not in price.summary


def test_known_staff_phone_is_internal():
    phone = "918590601638"
    verdict = assess_enquiry(
        service_category="sales",
        outline={"conversation_status": "New Enquiry", "service_category": "sales"},
        messages=[_msg("in", "Meeting details and urgent calls")],
        customer_name="BIJU",
        customer_phone=phone,
        staff_phones={phone_key(phone)},
        staff_first={"biju"},
    )
    assert verdict.include is False
    assert verdict.lane == "internal"


def test_partial_prices_keep_only_unanswered_and_full_reply_drops():
    open_one = assess_enquiry(
        messages=[
            _msg("in", "Need price of ECC5, platform 100kg and MSB"),
            _msg("out", "ECC5 price is Rs 12000. Platform 100kg is Rs 8000."),
        ],
        customer_name="ELMASS",
    )
    assert open_one.include is True
    assert open_one.unanswered == ("MSB",)

    closed = assess_enquiry(
        messages=[
            _msg("in", "Need price of ECC5 and MSB"),
            _msg("out", "ECC5 is Rs 12000 and MSB is Rs 9000"),
        ],
        customer_name="ELMASS",
    )
    assert closed.include is False
    assert closed.lane == "answered"


def test_outline_fallback_includes_awaiting_sales_status_without_messages():
    verdict = assess_enquiry_outline(
        service_category="sales",
        outline={
            "conversation_status": "New Enquiry",
            "service_category": "sales",
            "topic": "Weighing scale for shop",
        },
        customer_name="ELMASS",
        awaiting_staff_reply=True,
    )
    assert verdict.include is True
    assert verdict.lane == "sales_price"
    assert verdict.confidence == "low"

    still_open = assess_enquiry_outline(
        service_category="sales",
        outline={"conversation_status": "Interested", "service_category": "sales", "topic": "Demo"},
        customer_name="ELMASS",
        awaiting_staff_reply=False,
    )
    assert still_open.include is True
    assert still_open.lane == "sales_price"


def test_outline_fallback_quotation_required():
    verdict = assess_enquiry_outline(
        service_category="sales",
        outline={
            "conversation_status": "Quotation Required",
            "service_category": "sales",
            "topic": "Platform 100kg",
        },
        customer_name="Customer",
    )
    assert verdict.include is True
    assert "Platform 100kg" in verdict.summary or verdict.unanswered


def test_ov_camp_document_is_not_sales_enquiry_outline():
    verdict = assess_enquiry_outline(
        service_category="sales",
        outline={
            "conversation_status": "Awaiting Reply",
            "service_category": "sales",
            "topic": "OV urgent",
            "customer_intent": "Camp inspection — send OV certificate urgently",
            "current_issue": "Inspector called during camp check; OV document missing",
        },
        customer_name="Royal Kannur",
        customer_phone="919895339647",
        awaiting_staff_reply=True,
    )
    assert verdict.include is False
    assert verdict.lane == "stamping"

    with_messages = assess_enquiry(
        service_category="sales",
        outline={
            "conversation_status": "Awaiting Reply",
            "service_category": "sales",
            "topic": "OV urgent",
        },
        messages=[
            _msg("in", "Reached camp. Inspector called — please check and send OV document soon"),
            _msg("out", "Ok sir"),
            _msg("out", "Looking now — will send in 50 minutes"),
        ],
        customer_name="Royal Kannur",
        customer_phone="919895339647",
    )
    assert with_messages.include is False
    assert with_messages.lane == "stamping"


def test_otp_and_bare_code_is_service_not_enquiry():
    verdict = assess_enquiry(
        service_category="sales",
        outline={"conversation_status": "New Enquiry", "service_category": "sales"},
        messages=[
            _msg("in", "Hi"),
            _msg("in", "OTP"),
            _msg("in", "500"),
        ],
        customer_name="santhosh 44",
        customer_phone="91917305456997",
    )
    assert verdict.include is False
    assert verdict.lane == "service"
    assert verdict.outline_patch is not None
    assert verdict.outline_patch["service_category"] == "service"


def test_app_item_name_video_howto_is_service_not_enquiry():
    verdict = assess_enquiry(
        service_category="sales",
        outline={
            "conversation_status": "New Enquiry",
            "service_category": "sales",
            "topic": "Weighing app support",
            "customer_intent": "Video on manually changing item names",
        },
        messages=[
            _msg(
                "in",
                "Do you have a video on manually changing item names in app? Tough time changing items.",
            ),
            _msg("out", "Checking with admin about internet connection"),
        ],
        customer_name="yousufain meat Mart",
        customer_phone="916281402270",
    )
    assert verdict.include is False
    assert verdict.lane == "service"
    assert should_rewrite_outline(
        {"conversation_status": "New Enquiry", "service_category": "sales"},
        verdict,
    )

    outline_only = assess_enquiry_outline(
        service_category="sales",
        outline={
            "conversation_status": "Interested",
            "service_category": "sales",
            "customer_intent": "Needs usage video for changing product names in app",
        },
        customer_name="yousufain meat Mart",
        awaiting_staff_reply=True,
    )
    assert outline_only.include is False
    assert outline_only.lane == "service"


def test_cache_requires_same_fingerprint():
    fp = enquiry_fingerprint(
        outline_fp="abc",
        last_message_at="2026-11-25T00:00:00+00:00",
        awaiting=True,
        service_category="sales",
    )
    raw = assess_enquiry(
        messages=[_msg("in", "Price of ECC5")],
        customer_name="ELMASS",
    ).to_cache(fp)
    assert cache_from_json(raw, fp) is not None
    assert cache_from_json(raw, fp + "x") is None
