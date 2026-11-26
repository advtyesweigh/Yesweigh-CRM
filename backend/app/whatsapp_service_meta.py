"""Sales / service classification, priority, and customer hold clock for WhatsApp inbox."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.service import FOLLOWUP_PRIORITY_VALUES, normalize_outline_summary
from app.models import WhatsAppConversation, WhatsAppMessage

SERVICE_CATEGORY_VALUES = frozenset({"service", "sales", "cloud", "stamping"})

_SERVICE_KEYWORDS = re.compile(
    r"(?i)\b("
    r"service\s*charge|service\s*center|service\s*visit|technician|engineer\s*visit|"
    r"repair|repaired|fixing|broken|not\s*working|fault|defect|warranty|amc|"
    r"calibrat|installation|install|factory\s*reset|reset\s*device|"
    r"wifi|wi-?fi|wlan|connection\s*issue|motherboard|spare\s*part|dust\s*cover|"
    r"replacement\s*part|servicing|maintenance|onsite|visit\s*charge|"
    r"battery\s*issue|display\s*issue|printer\s*issue|weighing\s*scale\s*repair|"
    r"otp|one[-\s]?time\s*password|verification\s*code|login\s*code|pin\s*code|"
    r"item\s*names?|change\s+items?|manually\s+chang|how\s+to\s+use|software\s+training|"
    r"app\s+help|usage\s+video|training\s+video"
    r")\b",
)

_SALES_KEYWORDS = re.compile(
    r"(?i)\b("
    r"price|pricing|quotation|quote|rate|cost|how\s*much|brochure|catalogue|catalog|"
    r"demo|new\s*machine|purchase|buy\b|enquiry|inquiry|interested\s*in|"
    r"dealer\s*margin|dealer\s*price|dealer\s*prices|dealer\s*models?|dealer\s*enquiry|"
    r"dealer\s*inquiry|wholesale\s*price|distributor\s*price|become\s*a?\s*dealer|"
    r"retail\s*price|bulk\s*order|model\s*list|"
    r"product\s*list|specification|specs?\b|delivery\s*time|payment\s*terms|discount|"
    r"order(?:ing)?\s+new|machine\s+spec|sales\s*order|"
    r"courier|dispatch|billing\s*details|stamping\s*plate|"
    r"\d+\s*(?:nos|pcs|pieces|units)"
    r")\b",
)

_SALES_PHRASES = re.compile(
    r"(?i)(sales\s*order|order\s*confirmed|"
    r"quotation\s*required|new\s*enquiry|ordering\s+weighing|"
    r"weighing\s+machine[s]?\s+for|place\s+(?:the\s+)?order)",
)

_CLOUD_KEYWORDS = re.compile(
    r"(?i)\b("
    r"weighing\s*sync|ezee(?:\s*mart)?|cloud\s*recharge|ecs\s*cloud|ecs\s*lite|yes\s*cloud|"
    r"software\s*subscription|subscription\s*ended|subscription\s*validity|subscription\s*period|"
    r"unlimited\s*sales\s*logs|sales\s*sync|"
    r"pay\s*to\s*renew|renew\s*required|renew\s*subscription|per\s*scale|per\s*terminal|"
    r"cloud\s*plan|sync\s*subscription|standard\s*subscription|standard\s*mss|\bmss\b|"
    r"shop\s*id|shopid|license\s*renewal|interweighing"
    r")\b",
)

_CLOUD_MALAYALAM = re.compile(
    r"ലൈസൻസ്\s*പുതുക്കൽ|സോഫ്റ്റ്‌?വെയർ\s*സബ്‌?സ്‌?ക്രിപ്‌?ഷൻ\s*പുതുക്കുക|"
    r"സബ്സ്ക്രിപ്ഷൻ\s*പുതുക്കുക",
)

_CLOUD_PHRASES = re.compile(
    r"(?i)(renewal\s*required|pay\s+.*\s+per\s+scale|"
    r"please\s+pay\s+.*\s+renew|subscription\s+renew|cloud\s+recharge|"
    r"renew\s+subscription|shopid\s*=\s*\d+|could\s+not\s+find\s+shop\s+id|"
    r"weighing\s*sync|ecs\s*lite|sales\s*sync\s*update|subscription\s*validity|"
    r"standard\s*mss|\*mss\*|terminal\s*type\s*:\s*ecs)",
)

_STAMPING_KEYWORDS = re.compile(
    r"\b("
    r"ov\b|ov\s*certificate|ov\s*send|insitu\s*ov|"
    r"verification\s*certificate|certificate\s*of\s*verification|verification\s*copy|"
    r"verified\s+and\s+stamped|camp\s*stamp|camp\s*check|camp\s*inspection|"
    r"legal\s*metrology|metrology\s*stamp|"
    r"stamp\s*certificate|inspection\s*certificate|inspector\b|"
    r"\d+\s*kg\s*ov\b|kg\s*ov\b|"
    r"stamping\b|stamp\s*fee|stamp\s*charge|stamped\b|"
    r"sealed?\s*(?:paper|certificate|copy|document)|stamp(?:ed|ing)?\s*paper|"
    r"yesgatc\.in/download|"
    r"verification\s*paper|document\s*copy|2428\.pdf|ov\.pdf"
    r")\b",
    re.I,
)
_STAMPING_MALAYALAM = re.compile(
    r"ഓവി|പരിശോധന|സ്റ്റാമ്പ്|സ്റ്റാമ്പിങ്|ഇൻസ്പെക്ട|ക്യാമ്പ്|രേഖ|"
    r"ov\s*send\s*cheyth|ഒവി|"
    r"സീൽ(?:\s*ചെയ്ത)?(?:\s*പേപ്പർ)?|സീല്(?:\s*ചെയ്ത)?(?:\s*പേപ്പര്)?"
)
_STAMPING_PLATE = re.compile(r"(?i)stamping\s*plate")
# Serial + OV: "Y01277,Y01278 ov", "YZ01414… OV PLEASE SEND", "GO26/26,… OV", "Y10264,yzoo394 ov"
_STAMPING_SERIAL_OV = re.compile(
    r"(?:"
    r"\b(?:y|yo|yz)\s*0*\d{3,6}(?:\s*[,/;\-]+\s*(?:y|yo|yz)?\s*0*\d{3,6}){0,24}\s*[,.]?\s*ov\b"
    r"|\b(?:y|yo)\s+\d{3,6}\s+\d+\s*kg\s+ov\b"
    r"|\byzoo\s*\d{3,6}\s*ov\b"
    r"|\bgo\s*\d{1,3}\s*/\s*\d{1,3}\b.{0,160}\bov\b"
    r"|\bov\s*please\s*send\b"
    r")",
    re.I,
)
_STAMPING_AYACHILLA = re.compile(r"(?i)ayachilla|അയച്ചില്ല")
_STAMPING_SERIAL_TOKEN = re.compile(
    r"(?i)\b(?:y|yo|yz)\s*0*\d{3,6}\b|\bgo\s*\d{1,3}\s*/\s*\d{1,3}\b|\byzoo\s*\d{3,6}\b"
)
_STAMPING_PDF_NAME = re.compile(
    r"(?:^|[/\\\s])(?:ov|stamp|verification|certificate)[^/\\\s]*\.pdf\b",
    re.I,
)


def _norm_category(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if s == "support":
        return "sales"
    if s in SERVICE_CATEGORY_VALUES:
        return s
    return ""


def _norm_priority(raw: str | None) -> str:
    s = (raw or "").strip().title()
    if s in {"Med", "Mid"}:
        s = "Medium"
    return s if s in FOLLOWUP_PRIORITY_VALUES else ""


_STAMP_PRICE = re.compile(
    r"(?i)(?:including|with|without|\+)\s+stamping|stamping\s+and\s+gst|"
    r"stamping\s+charges?|how\s+much\s+with\s+stamping"
)


def stamp_price_only(blob: str) -> bool:
    """Price-list 'including stamping and GST' is not a certificate request."""
    text = blob or ""
    if not text.strip() or not _STAMP_PRICE.search(text):
        return False
    if _STAMPING_SERIAL_OV.search(text) or _STAMPING_PDF_NAME.search(text):
        return False
    if re.search(
        r"(?i)\b(ov\b|certificate|inspector|metrology|camp|sealed|verification)\b",
        text,
    ):
        return False
    if re.search(r"ഓവി|ഒവി|സ്റ്റാമ്പ്|സ്റ്റാമ്പിങ്|സീൽ", text):
        return False
    return True


def _stamp_text_without_plate(text: str) -> str:
    return _STAMPING_PLATE.sub(" ", text or "")


def _ayachilla_ov_context(text: str) -> bool:
    """Malayalam 'didn't send' in a serial/OV thread = pending OV certificate."""
    if not _STAMPING_AYACHILLA.search(text or ""):
        return False
    return bool(
        re.search(r"(?i)\bov\b|ഓവി|ഒവി", text or "")
        or _STAMPING_SERIAL_OV.search(text or "")
        or _STAMPING_SERIAL_TOKEN.search(text or "")
    )


def stamping_signal(blob: str) -> bool:
    """OV / camp verification / legal metrology stamping — not a price line or stamping plate."""
    text = blob or ""
    if not text.strip() or stamp_price_only(text):
        return False
    plate = bool(_STAMPING_PLATE.search(text))
    body = _stamp_text_without_plate(text) if plate else text
    serial = bool(_STAMPING_SERIAL_OV.search(text))
    pdf = bool(_STAMPING_PDF_NAME.search(text))
    mal = bool(_STAMPING_MALAYALAM.search(body))
    keywords = bool(_STAMPING_KEYWORDS.search(body))
    ayachilla = _ayachilla_ov_context(text)
    if plate and not (keywords or mal or serial or pdf or ayachilla):
        return False
    if keywords or mal or serial or pdf or ayachilla:
        return True
    if re.search(r"(?i)certificate\s+of\s+verification|verified\s+and\s+stamped", text):
        return True
    return False


def stamping_certificate_request(blob: str) -> bool:
    """Dealer asked for an old or new OV / stamp certificate, not a price quote."""
    text = blob or ""
    if not stamping_signal(text) or stamp_price_only(text):
        return False
    if _STAMPING_SERIAL_OV.search(text) or _STAMPING_PDF_NAME.search(text):
        return True
    if _ayachilla_ov_context(text):
        return True
    if re.search(r"ഓവി|ഒവി|സ്റ്റാമ്പ്|സ്റ്റാമ്പിങ്|സീൽ|ഇൻസ്പെക്ട|ക്യാമ്പ്", text):
        return True
    body = _stamp_text_without_plate(text)
    return bool(_STAMPING_KEYWORDS.search(body))


def stamping_keyword_tags(text: str) -> list[str]:
    blob = text or ""
    tags: list[str] = []
    if re.search(r"(?i)\bov\b|kg\s*ov|ഓവി|ഒവി", blob) or _STAMPING_SERIAL_OV.search(blob):
        tags.append("ov")
    if re.search(r"(?i)camp|ക്യാമ്പ്|inspector|inspection", blob):
        tags.append("camp")
    if re.search(r"(?i)stamping|stamp(?:ed|ing)?|സ്റ്റാമ്പ്|metrology|സീൽ|sealed?\s*paper", blob):
        tags.append("stamping")
    if re.search(
        r"(?i)verification|certificate\s+of\s+verification|verified\s+and\s+stamped|രേഖ|yesgatc\.in/download",
        blob,
    ):
        tags.append("verification")
    return tags[:4]


def _cloud_signal(blob: str) -> bool:
    if not blob:
        return False
    if _CLOUD_MALAYALAM.search(blob):
        return True
    if _CLOUD_KEYWORDS.search(blob) or _CLOUD_PHRASES.search(blob):
        return True
    if re.search(r"(?i)renew\s+subscription|shopid\s*=", blob):
        return True
    if re.search(r"(?i)subscription\s+rate|maximum\s+products|terminal\s+status", blob) and re.search(
        r"(?i)subscription|terminal|ecs|mss", blob
    ):
        return True
    return False


def heuristic_service_category(*texts: str | None) -> str:
    blob = " ".join((t or "").strip() for t in texts if (t or "").strip())
    if not blob:
        return ""
    cld = _cloud_signal(blob)
    svc = bool(_SERVICE_KEYWORDS.search(blob))
    sal = bool(_SALES_KEYWORDS.search(blob) or _SALES_PHRASES.search(blob))
    if cld and not (svc and re.search(r"(?i)(repair|technician|factory\s*reset|warranty\s*visit)", blob)):
        return "cloud"
    stamp = stamping_signal(blob)
    if stamp:
        strong_sales_only = sal and _SALES_PHRASES.search(blob) and not re.search(
            r"(?i)\b(ov|verification|inspector|camp|stamping|metrology)\b", blob
        )
        if not strong_sales_only:
            return "stamping"
    if sal and _SALES_PHRASES.search(blob):
        return "sales"
    if svc and not sal:
        return "service"
    if sal and not svc:
        return "sales"
    if svc and sal:
        if re.search(r"(?i)(sales\s*order|order\s*confirmed|quotation|purchase|dealer\s*price)", blob):
            return "sales"
        return "service"
    if re.search(r"(?i)service\s*required", blob):
        return "service"
    if re.search(r"(?i)(quotation\s*required|new\s*enquiry|order\s*confirmed)", blob):
        return "sales"
    if re.search(
        r"(?i)\b(otp|one[-\s]?time\s*password|verification\s*code|login\s*code)\b", blob
    ) and not sal:
        return "service"
    if re.search(
        r"(?i)\b(item\s*names?|change\s+items?|manually\s+chang|how\s+to\s+use|software\s+training)\b",
        blob,
    ) and not sal:
        return "service"
    if re.search(r"(?i)do\s+you\s+have.{0,40}\bvideo\b", blob) and re.search(
        r"(?i)\b(app|item|software|usage)\b", blob
    ) and not sal:
        return "service"
    return ""


def priority_from_summary(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    return _norm_priority(summary.get("followup_priority"))


def service_category_from_summary(summary: dict[str, Any] | None, *, fallback_text: str = "") -> str:
    if not summary:
        return heuristic_service_category(fallback_text)
    status = (summary.get("conversation_status") or "").lower()
    narrative = " ".join(
        str(summary.get(k) or "")
        for k in ("topic", "customer_intent", "current_issue", "recommended_action")
    )
    info = " ".join(summary.get("important_information") or [])
    product_blob = " ".join(str(p) for p in (summary.get("product_interest") or []))
    blob = " ".join(
        str(summary.get(k) or "")
        for k in ("topic", "customer_intent", "current_issue", "recommended_action", "conversation_status")
    )
    blob += " " + info + " " + product_blob

    def _cloud_wins(*extra: str | None) -> bool:
        combined = " ".join(filter(None, [blob, fallback_text, *extra]))
        return _cloud_signal(combined) or heuristic_service_category(blob, fallback_text, info, product_blob) == "cloud"

    if "renewal required" in status or _cloud_wins():
        if not (
            heuristic_service_category(blob, fallback_text) == "service"
            and not _cloud_signal(blob + " " + fallback_text)
        ):
            return "cloud"

    cat = _norm_category(summary.get("service_category"))
    combined = " ".join(filter(None, [blob, fallback_text]))
    narrative_blob = " ".join(filter(None, [narrative, info, fallback_text]))
    if (cat == "stamping" or stamping_signal(combined)) and not _cloud_wins():
        sales_order_only = re.search(r"(?i)(sales\s*order|order\s*confirmed)", combined) and not stamping_signal(
            narrative_blob
        )
        if not sales_order_only:
            return "stamping"
    if cat in {"sales", "service", "support"} and _cloud_wins():
        return "cloud"
    if cat == "service" and (
        heuristic_service_category(narrative, fallback_text, info, product_blob) == "cloud"
        or (
            "software" in product_blob.lower()
            and re.search(r"(?i)(renew|subscription|shop\s*id|shopid|cloud)", narrative + " " + info + " " + fallback_text)
        )
    ):
        return "cloud"
    if cat:
        return cat
    cat = heuristic_service_category(blob, fallback_text)
    if cat:
        return cat
    if "service required" in status:
        return "service"
    sales_statuses = (
        "new enquiry",
        "interested",
        "quotation required",
        "order confirmed",
        "payment pending",
    )
    if any(s in status for s in sales_statuses):
        if "payment pending" in status and _cloud_wins():
            return "cloud"
        return "sales"
    return ""


def parse_outline_lite(raw_outline: str | None) -> dict[str, Any]:
    """Fast outline read for Intelligence HTTP paths — json.loads only, no Pydantic.

    Full ``parse_cached_outline_summary`` validates via Pydantic (~15–30ms/row) and
    dominates Enquiry / order-cycle page loads when scanning hundreds of chats.
    """
    text = (raw_outline or "").strip()
    if not text or not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    cat = str(data.get("service_category") or "").strip().lower()
    if cat == "support":
        cat = "sales"
    if cat not in SERVICE_CATEGORY_VALUES:
        cat = ""
    products = data.get("product_interest")
    if not isinstance(products, list):
        products = [products] if products else []
    info = data.get("important_information")
    if not isinstance(info, list):
        info = [info] if info else []
    return {
        "topic": str(data.get("topic") or ""),
        "customer_intent": str(data.get("customer_intent") or ""),
        "current_issue": str(data.get("current_issue") or ""),
        "conversation_status": str(data.get("conversation_status") or "").strip(),
        "product_interest": [str(x).strip() for x in products if str(x or "").strip()],
        "important_information": [str(x).strip() for x in info if str(x or "").strip()],
        "recommended_action": str(data.get("recommended_action") or ""),
        "service_category": cat,
        "followup_priority": str(data.get("followup_priority") or ""),
        "enquiry_subtype": str(data.get("enquiry_subtype") or ""),
    }


def parse_cached_outline_summary(raw_outline: str | None) -> dict[str, Any]:
    text = (raw_outline or "").strip()
    if not text:
        return normalize_outline_summary({})
    if text.startswith("{"):
        try:
            return normalize_outline_summary(json.loads(text))
        except json.JSONDecodeError:
            return normalize_outline_summary({})
    return normalize_outline_summary(text)


def apply_service_meta_to_conversation(
    conv: WhatsAppConversation,
    summary: dict[str, Any] | None,
    *,
    fallback_text: str = "",
) -> None:
    cat = service_category_from_summary(summary, fallback_text=fallback_text)
    if not cat and (conv.service_category or "").strip().lower() == "support":
        cat = "sales"
    conv.service_category = cat
    prio = priority_from_summary(summary)
    if prio:
        conv.priority = prio
    elif cat and not (conv.priority or "").strip():
        conv.priority = "Medium"


def format_hold_clock(since: datetime | None, *, now: datetime | None = None) -> tuple[str | None, str | None]:
    if since is None:
        return None, None
    now = now or datetime.now(timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    secs = int((now - since).total_seconds())
    if secs < 0:
        return since.isoformat(), None
    if secs < 3600:
        return since.isoformat(), f"{max(1, secs // 60)}m"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return since.isoformat(), f"{h}h" + (f" {m}m" if m else "")
    d = secs // 86400
    h = (secs % 86400) // 3600
    return since.isoformat(), f"{d}d" + (f" {h}h" if h else "")


async def hold_since_map(
    db: AsyncSession,
    conversations: list[WhatsAppConversation],
) -> dict[int, datetime | None]:
    conv_ids = [c.id for c in conversations if (c.status or "") != "Resolved"]
    if not conv_ids:
        return {}

    in_rows = (
        await db.execute(
            select(
                WhatsAppMessage.conversation_id,
                func.max(WhatsAppMessage.created_at).label("ts"),
            )
            .where(
                WhatsAppMessage.conversation_id.in_(conv_ids),
                WhatsAppMessage.direction == "in",
            )
            .group_by(WhatsAppMessage.conversation_id)
        )
    ).all()
    last_in = {int(r.conversation_id): r.ts for r in in_rows if r.conversation_id is not None and r.ts}

    out_rows = (
        await db.execute(
            select(
                WhatsAppMessage.conversation_id,
                func.max(WhatsAppMessage.created_at).label("ts"),
            )
            .where(
                WhatsAppMessage.conversation_id.in_(conv_ids),
                WhatsAppMessage.direction == "out",
            )
            .group_by(WhatsAppMessage.conversation_id)
        )
    ).all()
    last_out = {int(r.conversation_id): r.ts for r in out_rows if r.conversation_id is not None and r.ts}

    out: dict[int, datetime | None] = {}
    for cid in conv_ids:
        li = last_in.get(cid)
        if not li:
            out[cid] = None
            continue
        lo = last_out.get(cid)
        out[cid] = None if lo is not None and lo >= li else li
    return out


def resolve_service_fields(
    conv: WhatsAppConversation,
    summary: dict[str, Any] | None,
    *,
    hold_since: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    fallback = " ".join(
        filter(
            None,
            [conv.last_message_preview or "", conv.tag or "", conv.notes_summary or ""],
        )
    )
    cat = _norm_category(getattr(conv, "service_category", None))
    if not cat:
        cat = service_category_from_summary(summary, fallback_text=fallback)

    prio = _norm_priority(getattr(conv, "priority", None))
    if not prio:
        prio = priority_from_summary(summary)

    hold_iso, hold_clock = format_hold_clock(hold_since, now=now)
    return {
        "service_category": cat or None,
        "service_priority": prio or None,
        "hold_since": hold_iso,
        "hold_clock": hold_clock,
        "on_hold": bool(hold_iso and hold_clock),
    }


async def enrich_conversation_rows(
    db: AsyncSession,
    rows: list[WhatsAppConversation],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not rows or len(rows) != len(items):
        return items
    holds = await hold_since_map(db, rows)
    now = datetime.now(timezone.utc)
    for conv, item in zip(rows, items, strict=True):
        summary = parse_cached_outline_summary(getattr(conv, "ai_outline", None) or "")
        item.update(
            resolve_service_fields(conv, summary, hold_since=holds.get(conv.id), now=now)
        )
    return items