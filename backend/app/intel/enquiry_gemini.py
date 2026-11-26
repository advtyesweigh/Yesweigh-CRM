"""One structured Gemini pass for ambiguous price asks. Worker only. Intel budget."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget import estimate_cost_inr
from app.ai.provider import get_provider
from app.config import get_settings
from app.intel.flags import ORG
from app.models import AiSettings, AiUsageLog

logger = logging.getLogger("yesweigh.intel")

_RESERVE = Decimal("0.15")
_SYSTEM = """You extract unanswered product price questions from a YesWeigh WhatsApp chat.
Return ONLY JSON:
{"lane":"sales_price|service|cloud|stamping|auto_reply|internal|other","all_answered":false,"unanswered_price_questions":["short product"]}
Rules:
- sales_price only for a dealer/customer asking product price or a quote that staff have not fully answered.
- If staff already gave a price for a product, do not list that product.
- If every price question was answered, all_answered true and unanswered_price_questions empty.
- Business auto-replies, opening-hours greetings, repair/complaint/service, cloud renewal, OV/verification/stamping/camp inspection paperwork, and staff-to-staff meetings or porter/parcel logistics are not sales_price.
- List only the products still missing a price. No procedures. No extra facts.
"""


def _parse(text: str) -> dict | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def _intel_room(db: AsyncSession) -> bool:
    row = (
        await db.execute(select(AiSettings).where(AiSettings.organization_id == ORG))
    ).scalar_one_or_none()
    if row is None or not bool(getattr(row, "intel_enabled", False)):
        return False
    today = date.today()
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    month_start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
    purpose = AiUsageLog.purpose.like("intel_%")

    async def _spent(since: datetime) -> Decimal:
        val = (
            await db.execute(
                select(func.coalesce(func.sum(AiUsageLog.estimated_cost_inr), 0)).where(
                    AiUsageLog.organization_id == ORG,
                    AiUsageLog.success.is_(True),
                    purpose,
                    AiUsageLog.created_at >= since,
                )
            )
        ).scalar_one()
        return Decimal(str(val or 0))

    daily = Decimal(str(getattr(row, "intel_daily_budget_inr", 10) or 10))
    monthly = Decimal(str(getattr(row, "intel_monthly_budget_inr", 150) or 150))
    if await _spent(day_start) + _RESERVE > daily:
        return False
    if await _spent(month_start) + _RESERVE > monthly:
        return False
    return True


async def refine_unanswered(
    db: AsyncSession,
    messages: list[dict[str, str]],
    *,
    conversation_id: int | None = None,
) -> dict | None:
    """Return parsed JSON or None when budget, key, or the model is unavailable."""
    if not await _intel_room(db):
        return None
    env = get_settings()
    if not (env.gemini_api_key or "").strip():
        return None
    lines: list[str] = []
    for msg in (messages or [])[-30:]:
        who = "Customer" if (msg.get("direction") or "") == "in" else "Staff"
        text = " ".join((msg.get("text") or "").split())
        if text:
            lines.append(f"{who}: {text[:400]}")
    if not lines:
        return None
    prompt = "Chat (oldest → newest):\n" + "\n".join(lines)[:6000]
    provider = get_provider(env.ai_provider or "gemini", env.gemini_api_key)
    model = env.ai_model or "gemini-2.5-flash-lite"
    try:
        resp = await provider.generate(
            model=model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_output_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("intel enquiry gemini skipped: %s", exc)
        return None
    actual = estimate_cost_inr(
        resp.usage.input_tokens,
        resp.usage.output_tokens,
        env,
        thoughts_tokens=int(getattr(resp.usage, "thoughts_tokens", 0) or 0),
    )
    db.add(
        AiUsageLog(
            organization_id=ORG,
            purpose="intel_enquiry",
            provider=env.ai_provider or "gemini",
            model=model,
            conversation_id=conversation_id,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            total_tokens=int(getattr(resp.usage, "total_tokens", 0) or 0),
            thoughts_tokens=int(getattr(resp.usage, "thoughts_tokens", 0) or 0),
            estimated_cost_inr=actual,
            success=True,
        )
    )
    await db.flush()
    return _parse(resp.text or "")
