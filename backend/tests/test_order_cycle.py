"""Sales-order / waiting / paid are chat stages, not lifetime Zoho invoices."""
from datetime import date
from types import SimpleNamespace

from app.intel.order_cycle import (
    classify_order_cycle,
    enquiry_blocked_by_order_cycle,
    status_patch_for_stage,
    zoho_settled_for_cycle,
)
from app.intel.queries import empty_funnel_shell
from app.whatsapp_service_meta import stamping_signal


def _msg(direction: str, body: str, **extra):
    row = {"direction": direction, "body": body, "media_name": "", "transcript": "", "media_type": ""}
    row.update(extra)
    return row


def test_confirmed_without_pdf_is_sales_order():
    cycle = classify_order_cycle(
        [_msg("in", "Order confirmed. Please send the sales order")],
        {"conversation_status": "New Enquiry"},
    )
    assert cycle is not None
    assert cycle.stage == "sales_order"
    assert enquiry_blocked_by_order_cycle(
        [_msg("in", "Order confirmed. Please send the sales order")],
        {"conversation_status": "New Enquiry"},
    )


def test_outline_order_confirmed_without_pdf():
    cycle = classify_order_cycle([], {"conversation_status": "Order Confirmed", "topic": "Platform scale"})
    assert cycle is not None
    assert cycle.stage == "sales_order"


def test_so_pdf_without_payment_is_waiting_even_if_outline_says_confirmed():
    cycle = classify_order_cycle(
        [_msg("out", "SO-21967.pdf", media_name="SO-21967.pdf", media_type="document")],
        {"conversation_status": "Order Confirmed"},
    )
    assert cycle is not None
    assert cycle.stage == "waiting"
    assert "21967" in cycle.summary
    assert "waiting for payment" in cycle.summary


def test_phonepe_after_so_is_paid():
    cycle = classify_order_cycle(
        [
            _msg("out", "SO-21967.pdf", media_name="SO-21967.pdf"),
            _msg("in", "Paid using PhonePe UPI."),
        ],
        {"conversation_status": "Payment Pending"},
    )
    assert cycle is not None
    assert cycle.stage == "paid"


def test_promise_to_pay_stays_waiting():
    cycle = classify_order_cycle(
        [
            _msg("out", "SO-21870.pdf", media_name="SO-21870.pdf"),
            _msg("in", "payment to be transfer Tomorrow"),
        ],
        {},
    )
    assert cycle is not None
    assert cycle.stage == "waiting"


def test_renewal_please_pay_is_not_an_order_stage():
    cycle = classify_order_cycle(
        [_msg("out", "Please pay ₹2,242 per scale to renew Ezee Mart")],
        {"conversation_status": "Payment Pending", "topic": "subscription renew"},
    )
    assert cycle is None


def test_later_payment_request_beats_an_older_receipt():
    cycle = classify_order_cycle(
        [
            _msg("out", "SO-22001.pdf", media_name="SO-22001.pdf"),
            _msg("in", "Paid using Google Pay"),
            _msg("out", "Please pay the balance"),
        ],
        {},
    )
    assert cycle is not None
    assert cycle.stage == "waiting"


def test_zoho_settled_only_inside_the_so_window():
    sent = date(2026, 11, 20)
    old_paid = SimpleNamespace(document_type="invoice", status="closed", balance=0, date="2020-01-01")
    assert zoho_settled_for_cycle([old_paid], sent) is None
    recent_paid = SimpleNamespace(document_type="invoice", status="closed", balance=0, date="2026-11-21")
    assert zoho_settled_for_cycle([old_paid, recent_paid], sent) is True
    recent_open = SimpleNamespace(document_type="invoice", status="overdue", balance=1500, date="2026-11-22")
    assert zoho_settled_for_cycle([recent_paid, recent_open], sent) is False
    # Chat still waiting overrides a settled Zoho row when the dealer has not paid yet.
    cycle = classify_order_cycle(
        [
            _msg("out", "SO-21900.pdf", media_name="SO-21900.pdf"),
            _msg("in", "need payment today to confirm order"),
        ],
        {},
        zoho_settled=True,
    )
    assert cycle is not None
    assert cycle.stage == "waiting"


def test_spare_parts_chatter_is_not_a_confirmed_order():
    cycle = classify_order_cycle(
        [_msg("in", "need spare parts and add freight for the printer head")],
        {"conversation_status": "Awaiting Reply"},
    )
    assert cycle is None


def test_price_stamping_is_not_a_certificate_request():
    from app.whatsapp_service_meta import stamping_certificate_request

    assert stamping_signal("3186 including Stamping and gst") is False
    assert stamping_signal("How much with stamping?") is False
    assert stamping_signal("Yo 2428 50 kg ov") is True
    assert stamping_signal("please send the OV certificate") is True
    assert stamping_signal("stamping plate for the new scale") is False
    admali = (
        "GO26/26,,Y10334,,Y10303,Y10263,YZ01414,YZ00384,YZ00394,YO9962,Y10264,"
        "OV PLEASE SEND"
    )
    assert stamping_signal(admali) is True
    assert stamping_certificate_request(admali) is True
    assert stamping_signal("Y10264,yzoo394 ov") is True
    assert stamping_certificate_request("Y10264 yz00394 ov\nAyachilla") is True


def test_service_required_outline_is_not_rewritten():
    assert status_patch_for_stage({"conversation_status": "Service Required"}, "waiting") is None
    patched = status_patch_for_stage({"conversation_status": "Order Confirmed"}, "waiting")
    assert patched is not None
    assert patched["conversation_status"] == "Payment Pending"


def test_funnel_labels_match_the_queues():
    payload = empty_funnel_shell()
    labels = {stage["key"]: stage["label"] for stage in payload["stages"]}
    assert labels["sales_order"] == "Sales order"
    assert labels["invoice_open"] == "Waiting"
    assert labels["invoice_settled"] == "Paid"
    assert "dispatch" not in str(payload).lower()
