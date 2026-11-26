"""Sales-enquiry gate for the Intelligence Enquiry tab.

Pure rules. Does not write ``awaiting_staff_reply`` or call Gemini.
Unanswered product prices are what the Enquiry summary should show.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.ai.whatsapp_send_guards import phone_key
from app.whatsapp_reply_needed import is_customer_business_auto_reply, is_short_ack
from app.intel.order_cycle import enquiry_blocked_by_order_cycle
from app.whatsapp_service_meta import stamp_price_only, stamping_signal

RULES_VERSION = 10

_SALES_STATUSES = {"New Enquiry", "Interested", "Awaiting Reply", "Quotation Required"}
_CLOSED_STATUSES = {"Resolved", "Not Interested", "Order Confirmed", "Payment Pending", "Paid"}

_PRICE_CUE = re.compile(
    r"(?i)\b(price|prices|pricing|rate|rates|quotation|quote|how\s*much|dealer\s*price|wholesale)\b"
    r"|(?:^|[\s,.:;])വില(?:[\s,.:;]|$)|(?:^|[\s,.:;])നിരക്ക്(?:[\s,.:;]|$)"
)
_NON_SALES_OPS = re.compile(
    r"(?i)\b("
    r"ov\b|verification|certificate|inspector|inspection|camp\b|metrology|stamping|"
    r"license\s*copy|document\s*copy"
    r")\b|രേഖ|പരിശോധ|ഇൻസ്പെക്ട|ക്യാമ്പ"
)
_PRODUCT_HINT = re.compile(
    r"(?i)\b("
    r"scale|weighing|ecc|msb|ecs|platform|machine|model|balan|dealer|demo|"
    r"quotation|new\s+machine|\d+\s*kg"
    r")\b"
)
_MONEY = re.compile(r"(?i)(?:₹|rs\.?|inr)\s*\d|\b\d[\d,]{2,}(?:\.\d+)?\s*/-")
_SERVICE = re.compile(
    r"(?i)\b("
    r"repair|not\s*working|machine\s*fault|fault|complaint|calibration|warranty|"
    r"service\s*visit|service\s*charge|spare\s*part|technician\s*visit"
    r")\b|പ്രശ്നം|പരാതി|റിപ്പയർ"
)
_OTP_CREDENTIAL = re.compile(
    r"(?i)\b(otp|one[-\s]?time\s*password|verification\s*code|login\s*code|auth\s*code|pin\s*code)\b"
)
_APP_HOWTO = re.compile(
    r"(?:"
    r"\b("
    r"item\s*names?|change\s+(?:the\s+)?items?|changing\s+items?|manually\s+chang|"
    r"product\s*names?\s+in\s+(?:the\s+)?app|how\s+to\s+(?:use|change|edit)|"
    r"software\s+training|usage\s+video|training\s+video|app\s+(?:help|support|usage)|"
    r"edit\s+items?|rename\s+items?|tough\s+time\s+chang"
    r")\b"
    r"|\b(?:do\s+you\s+have|send\s+(?:me\s+)?(?:a\s+)?|need\s+(?:a\s+)?).{0,60}\bvideo\b.{0,40}\b(?:app|software|item|usage|how\s+to)"
    r")",
    re.I,
)
_PURCHASE = re.compile(
    r"(?i)\b(dealer\s*price|new\s*machine|place\s+(?:an?\s+)?order|buy\s+(?:a\s+)?(?:new\s+)?(?:scale|machine)|"
    r"quotation\s+for)\b"
)
_CLOUD = re.compile(
    r"(?i)\b(weighing\s*sync|subscription\s*renew|renew\s*subscription|cloud\s*recharge|ecs\s*lite)\b"
    r"|ലൈസൻസ്\s*പുതുക്കൽ"
)
_INTERNAL = re.compile(
    r"(?i)\b("
    r"meeting|meetings|attend(?:\s+the)?\s+meeting|urgent\s+call|call\s*back|callback|"
    r"contact\s+(?:the\s+)?customer|internal\s+ops|porter|parcel|consignment"
    r")\b|യോഗം|മീറ്റിംഗ്|അടിയന്തിര|ഉപഭോക്താവ"
)
_LOGISTICS = re.compile(r"(?i)\b(porter|parcel|consignment)\b")
_TECHNICIAN = re.compile(r"(?i)\btechnicians?\b")
_SPLIT = re.compile(r"\s*(?:,|;|\n|/|\band\b|&|\+)\s*", re.I)
_WORD = re.compile(r"[A-Za-z]{4,}")
_MEDIA = {
    "[media]",
    "media",
    "📷 photo",
    "🎥 video",
    "🎤 voice message",
    "📄 document",
    "sticker",
    "attachment",
}


@dataclass(frozen=True)
class EnquiryVerdict:
    include: bool
    lane: str
    unanswered: tuple[str, ...]
    summary: str
    needs_gemini: bool
    confidence: str
    outline_patch: dict[str, Any] | None

    def to_cache(self, fingerprint: str) -> str:
        return json.dumps(
            {
                "v": RULES_VERSION,
                "fp": fingerprint,
                "include": self.include,
                "lane": self.lane,
                "unanswered": list(self.unanswered),
                "summary": self.summary,
                "needs_gemini": self.needs_gemini,
                "confidence": self.confidence,
            },
            ensure_ascii=False,
        )


def enquiry_fingerprint(
    *,
    outline_fp: str,
    last_message_at: str,
    awaiting: bool,
    service_category: str,
) -> str:
    return f"v{RULES_VERSION}|{outline_fp}|{last_message_at}|{int(bool(awaiting))}|{(service_category or '').lower()}"


def staff_first_names(names: list[str] | tuple[str, ...]) -> set[str]:
    """First Latin token (4+ letters) of each employee name. ``Biju B`` → ``biju``."""
    out: set[str] = set()
    for raw in names:
        parts = _WORD.findall(raw or "")
        if parts:
            out.add(parts[0].lower())
    return out


def software_support_service_signal(blob: str) -> bool:
    """OTP/login codes and in-app how-to / usage video — not sales price enquiries."""
    text = (blob or "").strip()
    if not text:
        return False
    has_price = bool(_PRICE_CUE.search(text) or _PURCHASE.search(text))
    if _OTP_CREDENTIAL.search(text) and not has_price:
        return True
    if _APP_HOWTO.search(text):
        if has_price and _PURCHASE.search(text) and not re.search(
            r"(?i)\b(app|item\s*name|software|usage|video)\b", text
        ):
            return False
        return not (_PRICE_CUE.search(text) and _PRODUCT_HINT.search(text) and _MONEY.search(text))
    return False


def _outline_blob(summary: dict[str, Any] | None) -> str:
    data = summary or {}
    bits: list[str] = []
    for key in ("topic", "customer_intent", "current_issue", "recommended_action", "conversation_status"):
        bit = str(data.get(key) or "").strip()
        if bit:
            bits.append(bit)
    for key in ("product_interest", "important_information"):
        for item in data.get(key) or []:
            text = str(item or "").strip()
            if text:
                bits.append(text)
    return " ".join(bits)


def _norm_cat(value: str | None) -> str:
    cat = (value or "").strip().lower()
    if cat == "support":
        return "sales"
    return cat


def _name_tokens(name: str) -> list[str]:
    return [p.lower() for p in _WORD.findall(name or "")]


def _matches_staff_first(name: str, first_names: set[str]) -> bool:
    tokens = _name_tokens(name)
    if not tokens or len(tokens) > 4 or not first_names:
        return False
    return any(tok in first_names for tok in tokens)


def _is_placeholder(text: str) -> bool:
    return (text or "").strip().lower() in _MEDIA


def substantive_inbound(messages: list[dict[str, str]]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for idx, msg in enumerate(messages or []):
        if (msg.get("direction") or "") != "in":
            continue
        text = (msg.get("text") or "").strip()
        if not text or _is_placeholder(text) or is_short_ack(text):
            continue
        out.append((idx, text))
    return out


def _clean_product(chunk: str) -> str:
    cleaned = re.sub(
        r"(?i)\b(price|prices|pricing|rate|rates|quote|quotation|of|for|the|a|an|please|send|share|"
        r"what|is|how\s*much|cost|dealer|wholesale|me|us|your|വില|നിരക്ക്)\b",
        " ",
        chunk or "",
    )
    cleaned = re.sub(r"[?!.]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:;,.")
    return cleaned[:80]


def price_products(text: str) -> list[str]:
    """Product names asked in one customer price message. Empty if it is not a price ask."""
    raw = (text or "").strip()
    if not raw or not _PRICE_CUE.search(raw):
        return []
    match = re.search(r"(?i)\b(?:price|rate|quote|quotation|cost)\s+(?:of|for)\s+(.+)", raw)
    blob = match.group(1) if match else raw
    names: list[str] = []
    for part in _SPLIT.split(blob):
        name = _clean_product(part)
        if len(name) >= 2:
            names.append(name)
    if not names:
        trail = re.search(r"(?i)(.{2,40}?)\s+(?:price|rate|quotation|quote)\b", raw)
        if trail:
            name = _clean_product(trail.group(1))
            if len(name) >= 2:
                names.append(name)
    if not names:
        names.append("Dealer price" if re.search(r"(?i)dealer|wholesale", raw) else "Price")
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _product_answered(product: str, later_out: list[str]) -> bool:
    generic = product.strip().lower() in {"price", "dealer price"}
    key = _compact(product)
    for text in later_out:
        if generic:
            if _MONEY.search(text) or re.search(r"(?i)price\s*list|rate\s*list|quotation", text):
                return True
            continue
        if key and key in _compact(text) and (_MONEY.search(text) or re.search(r"(?i)\b(price|rate|quote)\b", text)):
            return True
    return False


def _price_label(product: str) -> str:
    label = (product or "").strip()
    if not label:
        return "Price"
    if re.search(r"(?i)\b(price|rate|quote)\b", label):
        return label[:80]
    if _NON_SALES_OPS.search(label) or not _sales_product_hint(label):
        return label[:80]
    return f"{label} price"


def _sales_product_hint(text: str) -> bool:
    blob = " ".join(str(text or "").split())
    if not blob or len(blob) < 2:
        return False
    if _NON_SALES_OPS.search(blob) and not _PRICE_CUE.search(blob):
        return False
    if _PRODUCT_HINT.search(blob):
        return True
    if _PRICE_CUE.search(blob):
        return True
    return False


def _patch_auto() -> dict[str, Any]:
    return {
        "topic": "Business auto-reply",
        "customer_intent": "Automated greeting",
        "current_issue": "",
        "conversation_status": "",
        "service_category": "",
        "enquiry_subtype": "",
        "product_interest": [],
        "recommended_action": "",
        "followup_priority": "",
        "important_information": [],
    }


def _patch_internal() -> dict[str, Any]:
    return {
        "topic": "Internal staff coordination",
        "customer_intent": "Staff logistics or meeting",
        "current_issue": "",
        "conversation_status": "",
        "service_category": "",
        "enquiry_subtype": "",
        "product_interest": [],
        "recommended_action": "",
        "followup_priority": "",
        "important_information": [],
    }


def _patch_service() -> dict[str, Any]:
    return {
        "conversation_status": "Service Required",
        "service_category": "service",
        "enquiry_subtype": "",
    }


def _patch_stamping() -> dict[str, Any]:
    return {
        "conversation_status": "Awaiting Reply",
        "service_category": "stamping",
        "enquiry_subtype": "",
    }


def _verdict(
    *,
    include: bool,
    lane: str,
    unanswered: list[str] | None = None,
    needs_gemini: bool = False,
    confidence: str = "high",
    patch: dict[str, Any] | None = None,
) -> EnquiryVerdict:
    items = tuple(unanswered or [])
    summary = ""
    if include and items:
        summary = " · ".join(_price_label(p) for p in items)[:240]
    return EnquiryVerdict(
        include=include,
        lane=lane,
        unanswered=items,
        summary=summary,
        needs_gemini=needs_gemini,
        confidence=confidence,
        outline_patch=patch,
    )


def _stamping_lane(cat: str, outline_cat: str, blob: str) -> bool:
    """Certificate / OV request. A price line that only mentions stamping is still sales."""
    if stamp_price_only(blob):
        return False
    if stamping_signal(blob):
        return True
    return cat == "stamping" or outline_cat == "stamping"


def cheap_exclude(
    *,
    service_category: str,
    outline: dict[str, Any] | None,
    customer_name: str,
    customer_phone: str,
    staff_phones: set[str] | None,
    staff_first: set[str] | None,
) -> str:
    """Lane name when the thread is obviously not an Enquiry row. Empty if messages are required."""
    summary = outline or {}
    status = str(summary.get("conversation_status") or "").strip()
    cat = _norm_cat(service_category) or _norm_cat(str(summary.get("service_category") or ""))
    outline_cat = _norm_cat(str(summary.get("service_category") or ""))
    blob = _outline_blob(summary)
    topic = str(summary.get("topic") or "").lower()
    intent = str(summary.get("customer_intent") or "").lower()
    if "auto-reply" in topic or "automated greeting" in intent:
        return "auto_reply"
    if status in _CLOSED_STATUSES or status == "Renewal Required":
        return "closed" if status != "Renewal Required" else "cloud"
    if cat == "cloud" or outline_cat == "cloud" or _CLOUD.search(blob):
        return "cloud"
    if _stamping_lane(cat, outline_cat, blob):
        return "stamping"
    phone = phone_key(customer_phone)
    if phone and staff_phones and phone in staff_phones:
        return "internal"
    price = bool(_PRICE_CUE.search(blob))
    ops = bool(_INTERNAL.search(blob))
    logistics = bool(_LOGISTICS.search(blob))
    if _TECHNICIAN.search(customer_name or "") and (logistics or (ops and not price)):
        return "internal"
    if staff_first and _matches_staff_first(customer_name, staff_first) and ops and not price:
        return "internal"
    if logistics and not price:
        return "internal"
    if _NON_SALES_OPS.search(blob) and not price and not stamp_price_only(blob):
        return "stamping"
    if software_support_service_signal(blob):
        return "service"
    if cat == "service" or outline_cat == "service" or status == "Service Required":
        return "service"
    if _SERVICE.search(blob) and not price and not _PURCHASE.search(blob):
        return "service"
    return ""


def _outline_products(summary: dict[str, Any]) -> list[str]:
    """Product / topic hints from AI outline when message scoring is not loaded yet."""
    data = summary or {}
    names: list[str] = []
    for key in ("product_interest", "important_information"):
        for item in data.get(key) or []:
            text = " ".join(str(item or "").split())[:80]
            if len(text) >= 2:
                names.append(text)
    topic = " ".join(str(data.get("topic") or "").split())[:80]
    skip = {"new enquiry", "sales enquiry", "quotation", "customer enquiry", "dealer enquiry"}
    if topic and topic.lower() not in skip and _sales_product_hint(topic):
        names.append(topic)
    intent = " ".join(str(data.get("customer_intent") or "").split())[:80]
    if intent and _PRICE_CUE.search(intent) and _sales_product_hint(intent):
        names.append(intent)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = _compact(name)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped[:8]


def assess_enquiry_outline(
    *,
    service_category: str = "",
    outline: dict[str, Any] | None = None,
    customer_name: str = "",
    customer_phone: str = "",
    staff_phones: set[str] | None = None,
    staff_first: set[str] | None = None,
    awaiting_staff_reply: bool = False,
) -> EnquiryVerdict:
    """Fast enquiry gate from outline + flags only (no message window). Used on cache miss during HTTP."""
    exclude_lane = cheap_exclude(
        service_category=service_category,
        outline=outline,
        customer_name=customer_name,
        customer_phone=customer_phone,
        staff_phones=staff_phones,
        staff_first=staff_first,
    )
    if exclude_lane:
        return _verdict(include=False, lane=exclude_lane)

    summary = outline or {}
    status = str(summary.get("conversation_status") or "").strip()
    if status not in _SALES_STATUSES:
        return _verdict(include=False, lane="other")

    blob = _outline_blob(summary)
    price_signal = bool(_PRICE_CUE.search(blob))
    products = _outline_products(summary)
    if status == "Quotation Required":
        open_items = products or ["Quotation"]
        return _verdict(
            include=True,
            lane="sales_price",
            unanswered=open_items,
            confidence="low",
            needs_gemini=True,
        )
    if price_signal:
        open_items = products or (["Dealer price"] if re.search(r"(?i)dealer|wholesale", blob) else ["Price"])
        return _verdict(
            include=True,
            lane="sales_price",
            unanswered=open_items,
            confidence="low",
            needs_gemini=not products,
        )
    if products:
        return _verdict(
            include=True,
            lane="sales_price",
            unanswered=products,
            confidence="low",
            needs_gemini=not awaiting_staff_reply,
        )
    return _verdict(include=False, lane="other")


def assess_enquiry(
    *,
    service_category: str = "",
    outline: dict[str, Any] | None = None,
    messages: list[dict[str, str]] | None = None,
    customer_name: str = "",
    customer_phone: str = "",
    staff_phones: set[str] | None = None,
    staff_first: set[str] | None = None,
    awaiting_staff_reply: bool = False,
) -> EnquiryVerdict:
    """Decide if this DM belongs on Enquiry, and which price asks are still open."""
    summary = outline or {}
    status = str(summary.get("conversation_status") or "").strip()
    cat = _norm_cat(service_category) or _norm_cat(str(summary.get("service_category") or ""))
    topic = str(summary.get("topic") or "").lower()
    intent = str(summary.get("customer_intent") or "").lower()
    if "auto-reply" in topic or "automated greeting" in intent:
        return _verdict(include=False, lane="auto_reply", patch=_patch_auto())
    phone = phone_key(customer_phone)
    if phone and staff_phones and phone in staff_phones:
        return _verdict(include=False, lane="internal", patch=_patch_internal())
    if cat == "cloud" or status == "Renewal Required":
        return _verdict(include=False, lane="cloud")
    if status in _CLOSED_STATUSES:
        return _verdict(include=False, lane="closed")
    if enquiry_blocked_by_order_cycle(messages or [], summary):
        return _verdict(include=False, lane="order_cycle")

    blob_early = _outline_blob(summary)
    outline_cat = _norm_cat(str(summary.get("service_category") or ""))
    if _stamping_lane(cat, outline_cat, blob_early):
        return _verdict(include=False, lane="stamping", patch=_patch_stamping())

    inbound = substantive_inbound(messages or [])
    inbound_text = [text for _, text in inbound]
    if inbound_text and all(is_customer_business_auto_reply(text) for text in inbound_text):
        return _verdict(include=False, lane="auto_reply", patch=_patch_auto())

    msg_blob = "\n".join((m.get("text") or "") for m in (messages or []))
    customer_blob = "\n".join(inbound_text)
    phone = phone_key(customer_phone)
    price_in_customer = bool(_PRICE_CUE.search(customer_blob))
    ops = bool(_INTERNAL.search(msg_blob) or _INTERNAL.search(_outline_blob(summary)))
    logistics = bool(_LOGISTICS.search(msg_blob) or _LOGISTICS.search(_outline_blob(summary)))
    if phone and staff_phones and phone in staff_phones:
        return _verdict(include=False, lane="internal", patch=_patch_internal())
    if _TECHNICIAN.search(customer_name or "") and (logistics or not price_in_customer):
        return _verdict(include=False, lane="internal", patch=_patch_internal())
    if staff_first and _matches_staff_first(customer_name, staff_first) and ops and not price_in_customer:
        return _verdict(include=False, lane="internal", patch=_patch_internal())
    if logistics and not price_in_customer:
        return _verdict(include=False, lane="internal", patch=_patch_internal())

    service_blob = f"{customer_blob}\n{_outline_blob(summary)}"
    cat = _norm_cat(service_category) or _norm_cat(str(summary.get("service_category") or ""))
    if _stamping_lane(cat, _norm_cat(str(summary.get("service_category") or "")), service_blob):
        return _verdict(include=False, lane="stamping", patch=_patch_stamping())
    if software_support_service_signal(f"{customer_blob}\n{service_blob}"):
        patch = _patch_service() if status in _SALES_STATUSES or cat == "sales" else None
        return _verdict(include=False, lane="service", patch=patch)
    if cat == "service" or status == "Service Required" or (
        _SERVICE.search(service_blob) and not _PURCHASE.search(customer_blob)
    ):
        patch = _patch_service() if status in _SALES_STATUSES or cat == "sales" else None
        if status == "Service Required" and cat == "service":
            patch = None
        return _verdict(include=False, lane="service", patch=patch)
    if cat == "cloud" or status == "Renewal Required" or _CLOUD.search(service_blob):
        return _verdict(include=False, lane="cloud")
    if status in _CLOSED_STATUSES:
        return _verdict(include=False, lane="closed")

    asks: list[tuple[str, int]] = []
    for idx, text in inbound:
        if is_customer_business_auto_reply(text):
            continue
        for product in price_products(text):
            asks.append((product, idx))
    if not asks:
        return _verdict(include=False, lane="other")

    outs = messages or []
    open_products: list[str] = []
    seen: set[str] = set()
    ambiguous = False
    any_money = any(_MONEY.search(m.get("text") or "") for m in outs if (m.get("direction") or "") == "out")
    for product, idx in asks:
        key = _compact(product)
        if key in seen:
            continue
        seen.add(key)
        later = [
            (m.get("text") or "")
            for i, m in enumerate(outs)
            if i > idx and (m.get("direction") or "") == "out" and (m.get("text") or "").strip()
        ]
        if _product_answered(product, later):
            continue
        open_products.append(product)
        if any_money and product.lower() not in {"price", "dealer price"}:
            if not any(_compact(product) in _compact(t) for t in later):
                ambiguous = True

    if not open_products:
        return _verdict(include=False, lane="answered")

    # A trailing business auto-reply does not answer prices and is not itself an enquiry.
    _ = awaiting_staff_reply
    return _verdict(
        include=True,
        lane="sales_price",
        unanswered=open_products[:8],
        needs_gemini=ambiguous,
        confidence="low" if ambiguous else "high",
    )


def should_rewrite_outline(summary: dict[str, Any] | None, verdict: EnquiryVerdict) -> bool:
    patch = verdict.outline_patch
    if not patch:
        return False
    data = summary or {}
    status = str(data.get("conversation_status") or "").strip()
    cat = _norm_cat(str(data.get("service_category") or ""))
    topic = str(data.get("topic") or "").strip()
    if verdict.lane in {"auto_reply", "internal"}:
        if status in _SALES_STATUSES or cat == "sales":
            return True
        return topic != str(patch.get("topic") or "")
    if verdict.lane == "service":
        return status in _SALES_STATUSES or cat == "sales"
    return False


def patched_outline(summary: dict[str, Any] | None, verdict: EnquiryVerdict) -> dict[str, Any]:
    base = dict(summary or {})
    patch = verdict.outline_patch or {}
    base.update(patch)
    return base


def apply_model_verdict(current: EnquiryVerdict, data: dict[str, Any] | None) -> EnquiryVerdict:
    """Replace a low-confidence price split with one structured model result."""
    if not isinstance(data, dict):
        return current
    lane = str(data.get("lane") or "").strip().lower()
    if lane in {"service", "cloud", "stamping", "auto_reply", "internal", "other"}:
        patch = {
            "auto_reply": _patch_auto(),
            "internal": _patch_internal(),
            "service": _patch_service(),
            "stamping": _patch_stamping(),
        }.get(lane)
        return _verdict(include=False, lane=lane, patch=patch, confidence="high")
    if lane != "sales_price":
        return current
    if data.get("all_answered"):
        return _verdict(include=False, lane="answered", confidence="high")
    items: list[str] = []
    for raw in data.get("unanswered_price_questions") or []:
        text = " ".join(str(raw or "").split())[:80]
        if text and text not in items:
            items.append(text)
        if len(items) >= 8:
            break
    if not items:
        return _verdict(include=False, lane="answered", confidence="high")
    return _verdict(include=True, lane="sales_price", unanswered=items, confidence="high")


def cache_from_json(raw: str | None, fingerprint: str) -> EnquiryVerdict | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("v") or 0) != RULES_VERSION or str(data.get("fp") or "") != fingerprint:
        return None
    unanswered = [str(x).strip() for x in (data.get("unanswered") or []) if str(x).strip()]
    include = bool(data.get("include"))
    summary = str(data.get("summary") or "")
    if include and not summary and unanswered:
        summary = " · ".join(_price_label(p) for p in unanswered)[:240]
    return EnquiryVerdict(
        include=include,
        lane=str(data.get("lane") or ""),
        unanswered=tuple(unanswered),
        summary=summary,
        needs_gemini=bool(data.get("needs_gemini")),
        confidence=str(data.get("confidence") or "high"),
        outline_patch=None,
    )
