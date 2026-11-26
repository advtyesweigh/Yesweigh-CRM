"""Decide whether a 1:1 WhatsApp thread still needs a human staff reply.

Short customer acks (ok, thanks, ശരി, 👍) after staff instructions are treated as
closed — unread cleared and excluded from inbox unreplied counts.

Inbound messages that are the *customer's contact* auto-reply / business-hours greeting
(thank you for contacting, welcomes you / how we can help, we're open daily 8–9,
out of office, etc.) are not queued for staff — same as acks.

UPI / Google Pay / PhonePe / Paytm payment confirmation text (or captioned screenshots)
are treated as payment acks — clear awaiting + unread; not “needs staff reply”.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.evolution import clear_conversation_unread
from app.whatsapp_chat_types import is_group_jid
from app.whatsapp_outline import load_recent_messages, message_text_for_outline

if TYPE_CHECKING:
    from app.models import WhatsAppConversation, WhatsAppMessage

_NON_ALNUM = re.compile(r"[^\w\u0d00-\u0d7f\u0980-\u09ff\s+]", re.UNICODE)
_SPACE = re.compile(r"\s+")
# Ack / reaction emojis (single or repeated → read+acked, never unreplied).
_ACK_EMOJI = frozenset(
    {
        "👍",
        "👎",  # still a reaction, not a staff-reply ask
        "👌",
        "🙏",
        "✅",
        "✔",
        "✔️",
        "☑",
        "🆗",
        "💯",
        "👏",
        "🤝",
        "😊",
        "🙂",
        "😀",
        "😁",
        "😄",
        "😃",
        "😉",
        "😌",
        "🥰",
        "😍",
        "😘",
        "🤗",
        "🫡",
        "💪",
        "✌️",
        "✌",
        "🤟",
        "🤩",
        "✨",
        "⭐",
        "🌟",
        "🔥",
        "❤️",
        "❤",
        "♥️",
        "♥",
        "💙",
        "💚",
        "💛",
        "💜",
        "🧡",
        "🖤",
        "🤍",
        "🤎",
        "🌹",
        "🎉",
        "🎊",
    }
)
_THUMBS = _ACK_EMOJI  # alias used by normalize
# Strip ZWJ / variation selectors / whitespace when checking emoji-only.
_EMOJI_NOISE = re.compile(r"[\s\u200d\ufe0e\ufe0f\u200b-\u200f\u2060]+", re.UNICODE)
# Any remaining non-emoji symbol after removing ack emoji → not emoji-only.
_EMOJI_CHAR = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols & pictographs + extended-A
    "\U00002600-\U000027BF"  # misc + dingbats
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002300-\U000023FF"  # misc technical (⏳ etc.)
    "]+",
    re.UNICODE,
)
_MEDIA_OUTBOUND = frozenset(
    {"video", "image", "document", "sticker", "gif", "ptt", "audio", "voice", "photo"}
)
_ACK_PHRASES = frozenset(
    {
        "ok",
        "okay",
        "okk",
        "oke",
        "okey",
        "o",  # common one-letter "ok"
        "k",
        "kk",
        "ok sir",
        "ok mam",
        "ok maam",
        "ok madam",
        "ok done",
        "ok bro",
        "okay bro",
        "done",
        "noted",
        "note",
        "fine",
        "yes",
        "ya",
        "yep",
        "yeah",
        "y",
        "sure",
        "right",
        "correct",
        "thanks",
        "thank you",
        "thankyou",
        "thx",
        "ty",
        "tq",
        "thank u",
        "many thanks",
        "received",
        "got it",
        "understood",
        "understand",
        "will do",
        "shall do",
        "as you say",
        "hai",
        "hi",
        "hello",
        # Malayalam — both അതെ and അതേ appear in real chats
        "ശരി",
        "ഓക്കേ",
        "ഒക്കെ",
        "അതെ",
        "അതേ",
        "സരി",
        "സാരി",
        "നന്ദി",
        "ഓക്കേ സർ",
        "ശരി സർ",
        "ഹായ്",
        "ഹായ",
        # Hindi / Hinglish
        "thik hai",
        "theek hai",
        "ji",
        "ji ok",
        "ha",
        "haan",
        "shukriya",
        "samajh gaya",
        "smaja",
    }
)
# Outline helper appends "\n[ml] …" translations — must not break ack matching.
_ML_TRANSLATION_LINE = re.compile(r"(?:^|\n)\s*\[ml\][^\n]*", re.I | re.UNICODE)
_PUNCT_ONLY = re.compile(r"^[\s.?!~…。？！,，\-–—_]+$")
_THANKS_ONLY = frozenset({"thanks", "thank you", "thankyou", "thx", "ty", "tq", "thank u", "many thanks", "നന്ദി", "shukriya"})
_STAFF_CLOSURE = re.compile(
    r"\b("
    r"please|kindly|contact|call|visit|meet|send|share|pay|payment|confirm|"
    r"note|check|inform|forward|connect|reach|coordinate|arrange|book|"
    r"try|attempt|do the|make sure|let me know once|let us know once|"
    r"സമ്പര്ക്ക|കോൾ|വിളി|അയയ്ക്ക|ചെയ്യ|വരിക"
    r")\b",
    re.I,
)
_OPEN_QUESTION = re.compile(
    r"\b(what|which|when|where|how much|how many|price|rate|quotation|quote|model|serial|"
    r"problem|issue|complaint|error|fault|broken|not working|help)\b",
    re.I | re.UNICODE,
)
_CUSTOMER_NEEDS = re.compile(
    r"\?|"
    r"\b(price|rate|quotation|quote|cost|amount|discount|problem|issue|complaint|"
    r"error|fault|broken|repair|service|visit|install|calibration|warranty|"
    r"order|payment|status|when|where|how|what|which|need|require|urgent|"
    r"വില|നിരക്ക്|പ്രശ്നം|പരാതി|സേവനം)\b",
    re.I | re.UNICODE,
)
# Real customer asks (narrower than _CUSTOMER_NEEDS — excludes "how can we help" templates).
_CUSTOMER_ASK = re.compile(
    r"\b(price|rate|quotation|quote|cost|amount|discount|problem|issue|complaint|"
    r"error|fault|broken|repair|install|calibration|warranty|"
    r"my order|my payment|send me|give me|i need|i want|"
    r"വില|നിരക്ക്|പ്രശ്നം|പരാതി)\b|"
    r"\?(?!\s*$)",  # question mark with following content (not lonely "??")
    re.I | re.UNICODE,
)
# Inbound from a business's WhatsApp "away / greeting" template (not our staff auto-send).
_STRONG_BUSINESS_AUTO_TEMPLATE = re.compile(
    r"thank you for contacting|thanks for contacting|thank you for reaching|thanks for reaching|"
    r"thanks for your message|thank you for your message|"
    r"thanks for getting in touch|thank you for getting in touch|"
    r"thank you for choosing|thank you for your interest|thanks for your interest|"
    r"please let us know how we can help|"
    # Word order varies: "how can we help" AND dealer templates "how we can help you"
    r"how (?:can|may) we (?:help|assist)(?:\s+you)?|"
    r"how we (?:can|may) (?:help|assist)(?:\s+you)?|"
    r"please share your (requirements|details|enquiry|inquiry)|"
    r"we (will|'ll) get back to you|get back to you (soon|shortly|as soon as)|"
    r"i(?:'ll| will) ask a representative|someone will (?:be in touch|respond)|"
    r"out of office|currently unavailable|away right now|on vacation|on leave|"
    r"we might be away|might be away|"
    r"leave a message|"
    r"auto(?:matic(?:ally)?)?[\s-]?reply|this is an automated message|"
    r"welcome to\b|"
    r"welcomes?\s+you\b|"
    r"your trusted partner|"
    r"for quick queries contact|"
    r"we (?:are )?available to serve you|"
    r"we look forward to|"
    r"happy shopping|"
    r"working time|"
    r"new location|"
    r"only support text messages|"
    r"kindly contact (?:our )?(?:sales|support|service|care) team|"
    r"please contact (?:our )?(?:sales|support|service|care) team|"
    r"contact (?:our )?sales team|"
    r"we deal in\b|"
    r"our (?:address|location)\b|"
    # Third-party broadcast / marketing spam (not a dealer asking us anything)
    r"mark your calendar|daily use (?:spoken )?words|daily use english|"
    r"important update:|dear customer|"
    r"added to (?:your )?account|explore the app now|"
    r"insurance quot|registration update|"
    r"unsupported message|"
    r"സമ്പർക്കിച്ചതിന് നന്ദി|ബന്ധപ്പെട്ടതിന് നന്ദി|സന്ദേശത്തിന് നന്ദി|"
    r"നിങ്ങളെ എങ്ങനെ സഹായിക്കാം|എങ്ങനെ സഹായിക്കാം|എങ്ങനെ സഹായിക്കാനാകും|"
    r"തിരികെ ബന്ധപ്പെടും|ഓഫീസ് സമയം|പ്രവർത്തന സമയം|"
    r"സ്വാഗതം",
    re.I | re.UNICODE,
)
# Dealer/customer shared UPI / GPay / PhonePe / Paytm payment confirmation (not a reply ask).
_PAYMENT_CONFIRM = re.compile(
    r"(?i)("
    r"paid\s+using(?:\s+(?:google\s*pay|gpay|phonepe|paytm|upi|\w+))?|"
    r"upi\s+transaction\s+id|"
    r"upi\s+ref(?:erence)?(?:\s*(?:no\.?|number|id|#))?|"
    r"payment\s+(?:successful|completed|done|credited)|"
    r"(?:txn|transaction)\s+(?:id|ref)[:\s#]*\d{6,}|"
    r"sent\s+you\s+(?:₹|rs\.?|inr|money|payment)|"
    r"you\s+paid\b|"
    r"amount\s+(?:has\s+been\s+)?(?:paid|transferred|credited)|"
    r"(?:i|we)\s+(?:have\s+)?paid\b|"
    r"gpay\.app\.goo\.gl|"
    r"പണമടച്ചു|പണം\s*അടച്ച|"
    r"ഗൂഗിൾ\s*പേ.{0,40}പണമ?|"
    r"ഫോൺപേ|ഫോണ്?പേ|"
    r"പേയ്മെന്റ്\s*നടത്ത"
    r")"
)
_PAYMENT_CONFIRM_SPAM = re.compile(
    r"(?i)(jee\s*\(?main\)?|niftee|nift\s+entrance|national\s+testing|"
    r"registration\s+form|രജിസ്ട്രേഷൻ)"
)
_BUSINESS_HOURS_BLAST = re.compile(
    r"(?:\b\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)\b.*?(?:to|\-|until)\s*\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)\b)|"
    r"(?:daily|every\s+day).{0,40}\b(?:open|close|closed)\b|"
    r"(?:all\s+)?(?:sundays?|mondays?|saturdays?).{0,50}\bopen\b|"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday).{0,40}"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|to)\b|"
    r"(?:office|business|working|store)\s+(?:hours?|time)|"
    r"working time|"
    r"(?:@\s*)?\d{1,2}\s*(?:am|pm)\s*(?:to|\-|–)\s*\d{1,2}\s*(?:am|pm)|"
    r"എല്ലാ ഞായറാഴ്ച|പ്രതിദിനം.{0,30}(?:തുറന്ന|അടച്ച)",
    re.I | re.UNICODE,
)
_BUSINESS_VOICE = re.compile(
    r"\b(we are|we're|our (team|store|shop|office|address|location)|visit us|call us|"
    r"hours of operation|business hours|working hours|working time|"
    r"welcome to|trusted partner|happy shopping|we deal in)\b|"
    r"\bഞങ്ങൾ\b|\bഞങ്ങളുടെ\b|\bസ്വാഗതം\b",
    re.I | re.UNICODE,
)
_STRIP_ML = re.compile(r"\n\[ml\][\s\S]*$", re.I)
# Location / Maps links often contain `?` — must not count as a customer question.
_URLS = re.compile(r"https?://\S+|www\.\S+", re.I | re.UNICODE)
_CUSTOMER_PERSONAL_INTENT = re.compile(
    r"\b(i |i'm |i need|i want|i would like|my order|my payment|my invoice|"
    r"can you send|send me|give me|please send|"
    r"enik |ente |ennu |enikk|"
    r"എനിക്ക്|എന്റെ|എനിക്കു്)\b",
    re.I | re.UNICODE,
)
# Business-card / signature auto-replies (name + phone/email + decorative blast).
_PHONE_LIKE = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-()]{7,}\d)(?!\d)")
_EMAIL_LIKE = re.compile(r"[\w.+-]+@[\w.-]+\.\w{2,}", re.I)
_DECOR_BLAST = re.compile(r"[✨⚡🌟⭐🔥💥✅✔️☑🆗💯👏🙏👍]+")


def _strip_ml_translation(text: str) -> str:
    """Drop outline `[ml] …` suffix so ack detection uses the real inbound body."""
    return _STRIP_ML.sub("", (text or "")).strip()


def _strip_urls(text: str) -> str:
    """Remove http(s)/www links so `?` in query strings is not treated as a question."""
    return _URLS.sub(" ", text or "")


def _msg_text(msg: WhatsAppMessage | None) -> str:
    """Inbound/outbound text for reply-needed (never ML-appended outline text)."""
    if not msg:
        return ""
    media_type = (getattr(msg, "media_type", None) or "").strip().lower()
    is_voice = media_type in {"audio", "ptt", "voice"} or bool(getattr(msg, "is_voice", False))
    transcript = (getattr(msg, "transcript", None) or "").strip()
    body = (getattr(msg, "body", None) or "").strip()
    if is_voice and transcript:
        return _strip_ml_translation(transcript)
    # Prefer raw body so "Ok" + stored ML translation does not break short-ack.
    if body and body not in {
        "[media]",
        "media",
        "📷 Photo",
        "🎥 Video",
        "🎤 Voice message",
        "📄 Document",
        "Sticker",
        "Attachment",
    }:
        return _strip_ml_translation(body)
    # Fallback for empty/placeholder outbound media captions.
    outlined = message_text_for_outline(msg) if msg else ""
    return _strip_ml_translation(outlined or body)


def _msg_media_type(msg: WhatsAppMessage | None) -> str:
    if not msg:
        return ""
    return (getattr(msg, "media_type", None) or "").strip().lower()


def is_emoji_only_ack(text: str) -> bool:
    """True for 👍 / 👍👍 / 🙏 / mixed reaction-only inbound (no words)."""
    raw = _strip_ml_translation(text)
    if not raw or len(raw) > 48:
        return False
    compact = _EMOJI_NOISE.sub("", raw)
    if not compact:
        return False
    # Prefer known ack emoji (👍👍, 🙏, etc.)
    rest = compact
    for emo in sorted(_ACK_EMOJI, key=len, reverse=True):
        rest = rest.replace(emo, "")
    if not rest:
        return True
    # Fallback: message is only emoji glyphs (no letters/digits)
    if _EMOJI_CHAR.fullmatch(compact) and not re.search(r"[\w\u0d00-\u0d7f\u0980-\u09ff]", compact, re.UNICODE):
        return True
    return False


def normalize_customer_text(text: str) -> str:
    raw = _strip_ml_translation(text)
    if not raw:
        return ""
    if is_emoji_only_ack(raw) or raw in _THUMBS:
        return "thumbsup"
    # Drop trailing/leading ack emoji from short phrases ("thanks 🙏")
    scrubbed = raw
    for emo in sorted(_ACK_EMOJI, key=len, reverse=True):
        scrubbed = scrubbed.replace(emo, " ")
    scrubbed = _EMOJI_NOISE.sub(" ", scrubbed).strip() or raw
    low = _NON_ALNUM.sub(" ", scrubbed.lower())
    return _SPACE.sub(" ", low).strip()


def _is_business_signature_card(text: str) -> bool:
    """Name/phone/email signature blasts (e.g. IMAGE Printings auto-reply)."""
    t = (text or "").strip()
    if not t or len(t) > 400:
        return False
    if _CUSTOMER_ASK.search(_strip_urls(t)):
        return False
    has_phone = bool(_PHONE_LIKE.search(t))
    has_email = bool(_EMAIL_LIKE.search(t))
    has_decor = bool(_DECOR_BLAST.search(t))
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    multi = len(lines) >= 2
    if has_phone and has_email:
        return True
    if multi and (has_phone or has_email) and has_decor:
        return True
    if multi and has_phone and has_decor and len(t) <= 220:
        return True
    return False


def is_customer_business_auto_reply(text: str) -> bool:
    """True when inbound is the contact's business greeting / away-hours template."""
    t = _strip_ml_translation(text)
    if not t or is_short_ack(t):
        return False
    # Genuine customer asks override template-looking fragments.
    if _CUSTOMER_PERSONAL_INTENT.search(t):
        return False
    # Ask detection ignores URLs — Maps/website links contain `?` query strings.
    t_ask = _strip_urls(t)
    customer_ask = bool(_CUSTOMER_ASK.search(t_ask))
    strong = bool(_STRONG_BUSINESS_AUTO_TEMPLATE.search(t))
    hours = bool(_BUSINESS_HOURS_BLAST.search(t))
    voice = bool(_BUSINESS_VOICE.search(t))
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    multi = len(lines) >= 2

    if _is_business_signature_card(t):
        return True
    # Welcome / away / bot / marketing templates — global for every agent.
    if strong and not (customer_ask and _CUSTOMER_PERSONAL_INTENT.search(t)):
        if hours or voice or multi or len(t) >= 18:
            return True
    if strong and re.search(
        r"please let us know|how (?:can|may) we (?:help|assist)|how we (?:can|may) (?:help|assist)|"
        r"welcome to|welcomes?\s+you|kindly contact|contact (?:our )?sales|"
        r"we deal in|our (?:address|location)|"
        r"thanks for getting in touch|thank you for contacting|thanks for contacting|"
        r"thank you for your interest|thank u for your interest|"
        r"unsupported message|important update|dear customer|mark your calendar",
        t,
        re.I,
    ):
        return True
    if hours and multi and len(t) >= 40 and not customer_ask:
        return True
    if hours and voice and len(t) >= 40:
        return True
    # "Hello Sir/Madam — We are Kerala's No.1 …" style marketing blasts.
    if (
        voice
        and multi
        and len(t) >= 60
        and not customer_ask
        and re.search(r"\b(hello|hi|dear)\b.*\b(sir|madam)\b|\bwe are\b.{0,80}\b(provider|dealer|supplier|company|store|shop)\b", t, re.I | re.S)
    ):
        return True
    # Business name + services card without a personal ask.
    if (
        multi
        and len(t) >= 40
        and not customer_ask
        and not _CUSTOMER_PERSONAL_INTENT.search(t)
        and re.search(r"\b(sales|service|weighing|machines?|deal in|all type)\b", t, re.I)
    ):
        return True
    # Lonely "??" / "." after a blast — not a business template; fall through.
    if customer_ask and not strong:
        return False
    if _CUSTOMER_NEEDS.search(t_ask) and not strong:
        return False
    # Staff-style open questions from a human customer (not "how can/we can help you").
    if _OPEN_QUESTION.search(t_ask):
        if not strong and not voice:
            return False
        if re.search(
            r"\b(how can we help|how may we help|how may we assist|how we can help|how we may help)\b",
            t,
            re.I,
        ):
            return True
        if not strong:
            return False

    if hours and strong:
        return True
    return False


def is_payment_confirmation(text: str) -> bool:
    """True when inbound is a UPI/GPay/PhonePe/Paytm payment ack (not a staff-reply ask)."""
    t = _strip_ml_translation(text)
    if not t or len(t) > 2500:
        return False
    if _PAYMENT_CONFIRM_SPAM.search(t):
        return False
    # Real asks about payment status / amount still need a human.
    if _CUSTOMER_PERSONAL_INTENT.search(t) and re.search(
        r"\b(when|where|how much|status|pending|failed|not\s+received|refund)\b",
        t,
        re.I,
    ):
        return False
    if _PAYMENT_CONFIRM.search(t):
        return True
    # Screenshot OCR / pasted receipt: Completed + UPI id / bank rail without a question.
    if (
        re.search(r"\bcompleted\b", t, re.I)
        and re.search(r"\b(upi|gpay|google\s*pay|phonepe|paytm|indusind|@okaxis|@ptyes|@ybl)\b", t, re.I)
        and not _CUSTOMER_ASK.search(t)
        and "?" not in t
    ):
        return True
    return False


def is_short_ack(text: str) -> bool:
    if is_emoji_only_ack(text):
        return True
    norm = normalize_customer_text(text)
    if not norm:
        return False
    if norm == "thumbsup":
        return True
    if norm in _ACK_PHRASES:
        return True
    # "ok sir," / "thanks 🙏" — short phrase made only of ack tokens
    tokens = norm.split()
    if not tokens or len(tokens) > 4:
        return False
    if len("".join(tokens)) > 32:
        return False
    return all(t in _ACK_PHRASES or t in {"sir", "madam", "maam", "mam", "sister", "bro"} for t in tokens)


def inbound_clearly_needs_reply(text: str) -> bool:
    t = _strip_ml_translation(text)
    if not t:
        return True
    if is_emoji_only_ack(t) or is_short_ack(t):
        return False
    # Lonely "??" / "..." is not a real customer ask.
    if _PUNCT_ONLY.match(t):
        return False
    t_ask = _strip_urls(t)
    if len(t_ask.strip()) > 100:
        return True
    if _CUSTOMER_ASK.search(t_ask):
        return True
    if _CUSTOMER_NEEDS.search(t_ask) and not _PUNCT_ONLY.match(t_ask):
        # Broad needs regex includes lone "?"; skip pure punctuation (handled above).
        if t_ask.strip("?") == "" or _PUNCT_ONLY.match(t_ask):
            return False
        return True
    if "?" in t_ask and len(t_ask) > 15:
        return True
    return False


def staff_message_invites_ack(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        # Empty caption — typically campaign media / sticker; treated as invite via
        # staff_outbound_invites_ack when media_type is known.
        return False
    if _OPEN_QUESTION.search(t) and not _STAFF_CLOSURE.search(t):
        return False
    if _STAFF_CLOSURE.search(t):
        return True
    if "?" in t and re.search(r"\b(can|could|will|would|shall|please|kindly|are you|do you)\b", t, re.I):
        return True
    return len(t) < 90 and "?" not in t


def staff_awaits_substantive_answer(text: str) -> bool:
    """True only for real staff questions — not product blurb that mentions price/model."""
    t = (text or "").strip()
    if not t:
        return False
    if staff_message_invites_ack(t):
        return False
    if "?" in t:
        return True
    # Interrogative lead-in (Which model… / What is…), not nouns mid-sentence.
    if re.match(
        r"(?is)\s*(what|which|when|where|who|how much|how many|do you|are you|can you|could you)\b",
        t,
    ):
        return True
    return False


def staff_outbound_invites_ack(msg: WhatsAppMessage | None) -> bool:
    """True when our last outbound (incl. campaign video) can be closed by a short ack."""
    if not msg:
        return False
    text = _msg_text(msg)
    media = _msg_media_type(msg)
    body_raw = (getattr(msg, "body", None) or "").strip()
    placeholder = body_raw in {
        "",
        "[media]",
        "media",
        "📷 Photo",
        "🎥 Video",
        "🎤 Voice message",
        "📄 Document",
        "Sticker",
        "Attachment",
    } or body_raw.startswith(("📷", "🎥", "📄", "🎤"))
    if media in _MEDIA_OUTBOUND or (placeholder and not text):
        # Capioned media with a real open question still needs a substantive answer.
        if text and _OPEN_QUESTION.search(text) and not _STAFF_CLOSURE.search(text):
            return False
        return True
    return staff_message_invites_ack(text)


def _is_skippable_ack_inbound(text: str) -> bool:
    t = _strip_ml_translation(text)
    if not t:
        return False
    if is_emoji_only_ack(t) or is_short_ack(t) or _PUNCT_ONLY.match(t):
        return True
    if is_customer_business_auto_reply(t):
        return True
    if is_payment_confirmation(t):
        return True
    return False


def _last_staff_outbound(recent_messages: list[WhatsAppMessage], inbound_body: str):
    """Most recent staff out before this inbound, skipping trailing ack/auto noise.

    Returns:
      msg — staff outbound found
      None — no outbound in window, OR a substantive inbound sits between ack and staff out
    """
    for m in reversed(recent_messages):
        if m.direction == "out":
            return m
        if m.direction != "in":
            continue
        text = _msg_text(m)
        if text == inbound_body or _is_skippable_ack_inbound(text):
            continue
        # Substantive customer message still pending above this ack.
        return None
    return None


def evaluate_inbound_reply_need(
    recent_messages: list[WhatsAppMessage],
    inbound_text: str,
) -> bool:
    """Return True when staff should still reply to the customer."""
    body = _strip_ml_translation(inbound_text)
    # Media placeholders are handled by caller via raw body; treat sticker/reaction labels as acks.
    if body.lower() in {"sticker", "reaction", "gif"}:
        return False
    # Emoji / punct-only are never unreplied.
    if is_emoji_only_ack(body) or _PUNCT_ONLY.match(body):
        return False
    if is_customer_business_auto_reply(body):
        return False
    if is_payment_confirmation(body):
        return False

    if is_short_ack(body):
        last_out = _last_staff_outbound(recent_messages, body)
        if last_out is None:
            # No staff out in window, or substantive inbound still pending above this ack.
            # If the *only* recent inbounds are acks (last_out None from empty history),
            # still clear — lone Ok/K/thanks as the latest message is not a staff-reply ask.
            for m in reversed(recent_messages):
                if m.direction == "in":
                    t = _msg_text(m)
                    if t == body or _is_skippable_ack_inbound(t):
                        continue
                    return True  # substantive inbound still open
                if m.direction == "out":
                    break
            return False
        out_text = _msg_text(last_out)
        if out_text and staff_awaits_substantive_answer(out_text):
            return True
        return False

    if inbound_clearly_needs_reply(body):
        return True

    # Soft greetings after our outbound media (Good morning / Hi alone already short-ack).
    last_out = _last_staff_outbound(recent_messages, body)
    if last_out is not None and staff_outbound_invites_ack(last_out) and len(body) <= 24:
        soft = normalize_customer_text(body)
        if soft in {"good morning", "good evening", "good afternoon", "gm", "ge", "namaste", "നമസ്കാരം"}:
            return False

    # Any non-ack inbound defaults to needing attention.
    if body:
        return True
    return True


def _is_dm(conv: WhatsAppConversation) -> bool:
    return not is_group_jid(conv.customer_jid or "")


def mark_staff_replied(conv: WhatsAppConversation) -> None:
    conv.awaiting_staff_reply = False
    conv.awaiting_since = None
    clear_conversation_unread(conv)


async def apply_inbound_reply_state(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    inbound_text: str = "",
    inbound_msg: WhatsAppMessage | None = None,
) -> bool:
    """Update awaiting_staff_reply + maybe clear unread after any inbound DM (all agents/providers)."""
    if getattr(conv, "is_spam", False):
        conv.awaiting_staff_reply = False
        conv.awaiting_since = None
        clear_conversation_unread(conv)
        return False
    if not _is_dm(conv):
        conv.awaiting_staff_reply = False
        return False

    text = (inbound_text or _msg_text(inbound_msg)).strip()
    recent = await load_recent_messages(db, conv, limit=12)
    if inbound_msg and inbound_msg not in recent:
        recent = [*recent, inbound_msg]

    needs = evaluate_inbound_reply_need(recent, text)
    conv.awaiting_staff_reply = needs
    from app.whatsapp_reply_latency import sync_awaiting_since

    at = None
    if inbound_msg and inbound_msg.created_at:
        at = inbound_msg.created_at
    sync_awaiting_since(conv, needs_reply=needs, at=at)
    if not needs:
        clear_conversation_unread(conv)
        if conv.status == "New":
            conv.status = "Open"
    return needs


def refresh_reply_state_from_messages(
    conv: WhatsAppConversation,
    messages: list[WhatsAppMessage],
) -> bool:
    """Recompute awaiting_staff_reply; clear unread when ack/auto/staff-out closes the window."""
    if getattr(conv, "is_spam", False) or not _is_dm(conv) or conv.status == "Resolved":
        conv.awaiting_staff_reply = False
        clear_conversation_unread(conv)
        return False
    last = None
    for m in reversed(messages):
        if m.direction in {"in", "out"}:
            last = m
            break
    if not last:
        conv.awaiting_staff_reply = False
        return False
    if last.direction == "out":
        # Staff already sent — not awaiting reply. Also drop stale green unread badges
        # when the prior inbound was only an ack/auto-reply (or any closed window).
        conv.awaiting_staff_reply = False
        from app.whatsapp_reply_latency import sync_awaiting_since

        sync_awaiting_since(conv, needs_reply=False, at=getattr(last, "created_at", None))
        # Clear unread unless the last inbound before this out still needs a human reply.
        prior_in = None
        for m in reversed(messages):
            if m is last:
                continue
            if m.direction == "in":
                prior_in = m
                break
            if m.direction == "out":
                break
        if prior_in is None:
            clear_conversation_unread(conv)
        else:
            prior_needs = evaluate_inbound_reply_need(messages, _msg_text(prior_in))
            if not prior_needs:
                clear_conversation_unread(conv)
        return False
    needs = evaluate_inbound_reply_need(messages, _msg_text(last))
    conv.awaiting_staff_reply = needs
    from app.whatsapp_reply_latency import sync_awaiting_since

    sync_awaiting_since(conv, needs_reply=needs, at=getattr(last, "created_at", None) or datetime.now(timezone.utc))
    if not needs:
        clear_conversation_unread(conv)
    return needs