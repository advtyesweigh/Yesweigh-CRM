from types import SimpleNamespace

from app.whatsapp_reply_needed import (
    evaluate_inbound_reply_need,
    is_customer_business_auto_reply,
    is_emoji_only_ack,
    is_payment_confirmation,
    is_short_ack,
    staff_message_invites_ack,
    staff_outbound_invites_ack,
)


def _msg(direction: str, body: str, media_type: str = "", transcript: str = ""):
    return SimpleNamespace(
        direction=direction,
        body=body,
        transcript=transcript,
        media_type=media_type,
    )


def test_short_ack_variants():
    assert is_short_ack("Ok")
    assert is_short_ack("ok sir")
    assert is_short_ack("ശരി")
    assert is_short_ack("അതെ")
    assert is_short_ack("അതേ")
    assert is_short_ack("👍")
    assert is_short_ack("👍👍")
    assert is_short_ack("🙏")
    assert is_short_ack("O")
    assert is_short_ack("Yes")
    assert is_emoji_only_ack("👍👍")
    assert is_emoji_only_ack("🙏")
    assert not is_short_ack("What is the price for 50kg?")


def test_ml_translation_suffix_does_not_break_ack():
    assert is_short_ack("Ok\n[ml] ശരി")
    assert is_short_ack("Yes\n[ml] അതെ")
    msgs = [_msg("out", "", "video"), _msg("in", "Ok\n[ml] ശരി")]
    assert not evaluate_inbound_reply_need(msgs, "Ok\n[ml] ശരി")


def test_emoji_after_campaign_video_clears_unreplied():
    for customer in ("👍👍", "🙏", "👍", "അതെ"):
        msgs = [_msg("out", "", "video"), _msg("in", customer)]
        assert staff_outbound_invites_ack(msgs[0])
        assert not evaluate_inbound_reply_need(msgs, customer)


def test_saifuddin_ok_after_contact_instruction():
    staff = "Can you please contact namrita sir"
    customer = "Ok"
    assert staff_message_invites_ack(staff)
    assert is_short_ack(customer)
    msgs = [_msg("out", staff), _msg("in", customer)]
    assert not evaluate_inbound_reply_need(msgs, customer)


def test_price_question_still_needs_reply():
    customer = "What is the rate for platform scale?"
    msgs = [_msg("in", customer)]
    assert evaluate_inbound_reply_need(msgs, customer)


def test_ok_after_open_price_question_still_needs_reply():
    staff = "Which model do you need?"
    customer = "ok"
    msgs = [_msg("out", staff), _msg("in", customer)]
    assert evaluate_inbound_reply_need(msgs, customer)


def test_bullsnet_style_business_auto_reply():
    customer = (
        "Thank you for contacting BULLSNET! Please let us know how we can help you. "
        "All Sundays are Open. Daily @ 8 am to 9 pm"
    )
    assert is_customer_business_auto_reply(customer)
    msgs = [_msg("in", customer)]
    assert not evaluate_inbound_reply_need(msgs, customer)


def test_welcome_and_away_auto_replies():
    samples = [
        "Welcome to HOPUZ PRINTERS & PACKAGERS\n\nYour Trusted Partner in Professional Printing",
        "Hey there! 👋 We might be away, but your ideas matter to us 💭\nLeave a message",
        "Thanks for getting in touch! I'll ask a representative to respond. Someone will be in touch.",
        "✅Working Time✅\n Monday to Saturday\n\n09:30 AM  to 07:00 PM\nNew Location",
        "Thank you for choosing city choice and happy shopping\n\nVisit Our Website",
    ]
    for customer in samples:
        assert is_customer_business_auto_reply(customer), customer[:40]
        assert not evaluate_inbound_reply_need([_msg("in", customer)], customer)


def test_image_printings_signature_auto_reply():
    customer = (
        "ഇമേജ് പ്രിന്റിങ്സ് ✨✨✨\n"
        "IMAGE Printings ⚡⚡⚡\n"
        "8078529528\n"
        "imagenilambur@gmail.com"
    )
    assert is_customer_business_auto_reply(customer)
    msgs = [_msg("out", "", "video"), _msg("in", customer)]
    assert not evaluate_inbound_reply_need(msgs, customer)


def test_punct_only_after_video_clears():
    msgs = [_msg("out", "", "video"), _msg("in", "??")]
    assert not evaluate_inbound_reply_need(msgs, "??")


def test_ok_clears_even_with_prior_auto_replies():
    msgs = [
        _msg("out", "", "video"),
        _msg("in", "Thank you for contacting eazyprint!"),
        _msg("in", "??"),
    ]
    assert not evaluate_inbound_reply_need(msgs, "??")


def test_ok_after_staff_spec_clears():
    msgs = [
        _msg("out", "Bench 50kg with printer\n200 items can add\nMobile app price 6700"),
        _msg("in", "Ok"),
    ]
    assert not evaluate_inbound_reply_need(msgs, "Ok")


def test_ok_does_not_hide_prior_voice_question():
    msgs = [
        _msg("out", "pls send photo"),
        _msg("in", "price for 50kg please", "audio"),
        _msg("in", "Ok"),
    ]
    # Substantive inbound still open above the trailing Ok.
    assert evaluate_inbound_reply_need(msgs, "Ok")


def test_short_thank_you_for_contacting():
    assert is_customer_business_auto_reply("Thank you for contacting Pranavi Enterprises")
    assert is_customer_business_auto_reply("Thank you for contacting അച്ചടിശാല!")


def test_thank_you_with_real_question_still_needs_reply():
    customer = "Thank you, what is the price for 50kg platform scale?"
    assert not is_customer_business_auto_reply(customer)
    assert evaluate_inbound_reply_need([_msg("in", customer)], customer)


def test_customer_personal_intent_not_auto_reply():
    customer = (
        "Thank you for contacting us. I need quotation for bench scale 30kg."
    )
    assert not is_customer_business_auto_reply(customer)


def test_orbit_electronics_welcome_auto_reply():
    """Dealer welcome template (EN word order 'how we can help') must not inflate unreplied."""
    customer = (
        "Orbit Electronics welcomes you! how we can help you.\n"
        "ഓർബിറ്റ് ഇലക്ട്രോണിക്സ് താങ്കളെ സ്വാഗതം ചെയ്യുന്നു! എങ്ങനെ സഹായിക്കാനാകും?"
    )
    assert is_customer_business_auto_reply(customer)
    # English-only body (ML may live in a separate bubble / translation field)
    en_only = "Orbit Electronics welcomes you! how we can help you."
    assert is_customer_business_auto_reply(en_only)
    # Campaign video may precede the welcome — latest inbound still clears.
    msgs = [_msg("in", "", "video"), _msg("in", en_only)]
    assert not evaluate_inbound_reply_need(msgs, en_only)


def test_gpay_payment_confirmation_clears_unreplied():
    customer = (
        "Paid using Google Pay ✅\n"
        "ഗൂഗിൾ പേ ഉപയോഗിച്ച് പണമടച്ചു. ✅"
    )
    assert is_payment_confirmation(customer)
    msgs = [_msg("out", "Please pay to confirm the order"), _msg("in", customer, "image")]
    assert not evaluate_inbound_reply_need(msgs, customer)


def test_upi_receipt_blob_clears_unreplied():
    customer = (
        "Completed\n26 Nov 2026, 10:04am\nIndusInd Bank 5743\n"
        "UPI transaction ID 633069436397\n"
        "To: INTERWEIGHING PRIVATE LIMITED\n"
        "From: SINEESH KUMAR N T"
    )
    assert is_payment_confirmation(customer)
    assert not evaluate_inbound_reply_need([_msg("in", customer, "image")], customer)


def test_payment_status_question_still_needs_reply():
    customer = "I paid yesterday — when will my payment status update?"
    assert not is_payment_confirmation(customer)
    assert evaluate_inbound_reply_need([_msg("in", customer)], customer)


def test_dealer_catalog_blast_with_maps_url_not_unreplied():
    """URL `?` query must not make a We-Deal-In catalog look like a customer ask."""
    customer = (
        "*We Deal In*\n"
        "Pouch Sealing Machines\n"
        "Bottle & Cup Sealing Machines\n"
        "Our Address:\nPlastoseal Packaging Solutions\n"
        "Our Location\nhttps://maps.google.com/?q=Plastoseal&hl=en"
    )
    assert is_customer_business_auto_reply(customer)
    assert not evaluate_inbound_reply_need([_msg("in", customer)], customer)


def test_kindly_contact_sales_team_auto_reply():
    customer = "Kindly contact sales team on 8281742233"
    assert is_customer_business_auto_reply(customer)
    assert not evaluate_inbound_reply_need([_msg("in", customer)], customer)
