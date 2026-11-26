"""AI budget helpers — reserve then settle actual cost."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import AiSettings, AiUsageLog, Employee, User

ORG = "yesweigh"
# Conservative reservation per command before actual token usage is known.
DEFAULT_RESERVE_INR = Decimal("0.50")

# Purpose buckets for Usage Overview split (WhatsApp AI vs AI Agent).
_AGENT_PURPOSES = frozenset({"command", "agent"})
_WHATSAPP_PURPOSES = frozenset(
    {
        "whatsapp_outline",
        "auto_reply",
        "lead_summary",
        "knowledge_search",
        "knowledge_learning",
    }
)


def purpose_bucket(purpose: str) -> str:
    p = (purpose or "").strip().lower()
    # Intel narration/ask uses ai_settings.intel_* caps (Phase B+). It must not
    # share the whatsapp_outline / command bucket.
    if p.startswith("intel_"):
        return "intel"
    if p in _AGENT_PURPOSES or p.startswith("agent"):
        return "agent"
    if p in _WHATSAPP_PURPOSES or p.startswith("whatsapp_"):
        return "whatsapp"
    return "other"


def estimate_cost_inr(
    input_tokens: int,
    output_tokens: int,
    settings: Settings | None = None,
    *,
    thoughts_tokens: int = 0,
) -> Decimal:
    s = settings or get_settings()
    # Gemini bills thinking/thoughts tokens like output.
    billed_out = int(output_tokens or 0) + int(thoughts_tokens or 0)
    usd = (input_tokens / 1_000_000.0) * s.ai_input_usd_per_1m + (billed_out / 1_000_000.0) * s.ai_output_usd_per_1m
    return Decimal(str(round(usd * s.ai_usd_inr, 6)))


async def get_or_create_settings(db: AsyncSession, *, for_update: bool = False) -> AiSettings:
    s = get_settings()
    q = select(AiSettings).where(AiSettings.organization_id == ORG)
    if for_update:
        q = q.with_for_update()
    row = (await db.execute(q)).scalar_one_or_none()
    if row:
        # Raise legacy low caps so list/report short replies are not token-starved.
        desired = max(1024, int(s.ai_max_output_tokens or 2048))
        if int(row.max_output_tokens or 0) < 1024:
            row.max_output_tokens = desired
        return row
    row = AiSettings(
        organization_id=ORG,
        enabled=bool(s.ai_enabled),
        provider=s.ai_provider or "gemini",
        model=s.ai_model or "gemini-3.5-flash-lite",
        monthly_budget_inr=Decimal(str(s.ai_monthly_budget_inr)),
        daily_budget_inr=Decimal(str(s.ai_daily_budget_inr)),
        max_output_tokens=int(s.ai_max_output_tokens),
        reserved_daily_inr=Decimal("0"),
        reserved_monthly_inr=Decimal("0"),
    )
    db.add(row)
    await db.flush()
    return row


async def spent_inr(db: AsyncSession, *, day: date | None = None, month_start: date | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(AiUsageLog.estimated_cost_inr), 0)).where(
        AiUsageLog.organization_id == ORG,
        AiUsageLog.success.is_(True),
    )
    if day is not None:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
        q = q.where(AiUsageLog.created_at >= start, AiUsageLog.created_at <= end)
    if month_start is not None:
        start = datetime(month_start.year, month_start.month, 1, tzinfo=timezone.utc)
        q = q.where(AiUsageLog.created_at >= start)
    val = (await db.execute(q)).scalar_one()
    return Decimal(str(val or 0))


class BudgetError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


async def reserve_budget(db: AsyncSession, amount: Decimal | None = None) -> Decimal:
    """Atomically reserve budget against daily/monthly limits. Raises BudgetError."""
    reserve = amount if amount is not None else DEFAULT_RESERVE_INR
    if reserve <= 0:
        reserve = DEFAULT_RESERVE_INR

    # Serialize org settings row
    settings_row = await get_or_create_settings(db, for_update=True)
    today = date.today()
    month_start = date(today.year, today.month, 1)

    day_spent = await spent_inr(db, day=today)
    month_spent = await spent_inr(db, month_start=month_start)

    # Reset stale reservations if spent caught up (simple hygiene)
    reserved_d = Decimal(str(settings_row.reserved_daily_inr or 0))
    reserved_m = Decimal(str(settings_row.reserved_monthly_inr or 0))

    daily_budget = Decimal(str(settings_row.daily_budget_inr))
    monthly_budget = Decimal(str(settings_row.monthly_budget_inr))

    if day_spent + reserved_d + reserve > daily_budget:
        raise BudgetError(
            f"Daily AI budget exhausted (₹{day_spent:.2f} used / ₹{daily_budget:.2f} limit). "
            "Rule-based CRM work continues; paid AI is paused."
        )
    if month_spent + reserved_m + reserve > monthly_budget:
        raise BudgetError(
            f"Monthly AI budget exhausted (₹{month_spent:.2f} used / ₹{monthly_budget:.2f} limit). "
            "Rule-based CRM work continues; paid AI is paused."
        )

    settings_row.reserved_daily_inr = reserved_d + reserve
    settings_row.reserved_monthly_inr = reserved_m + reserve
    await db.flush()
    return reserve


async def settle_reservation(db: AsyncSession, reserved: Decimal, actual: Decimal) -> None:
    """Release reservation and keep books consistent (usage log holds actual spend)."""
    settings_row = await get_or_create_settings(db, for_update=True)
    reserved_d = Decimal(str(settings_row.reserved_daily_inr or 0))
    reserved_m = Decimal(str(settings_row.reserved_monthly_inr or 0))
    settings_row.reserved_daily_inr = max(Decimal("0"), reserved_d - reserved)
    settings_row.reserved_monthly_inr = max(Decimal("0"), reserved_m - reserved)
    _ = actual  # recorded on AiUsageLog
    await db.flush()


async def usage_summary(db: AsyncSession, *, recent_limit: int = 50) -> dict:
    settings_row = await get_or_create_settings(db)
    today = date.today()
    month_start = date(today.year, today.month, 1)
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    month_start_dt = datetime(month_start.year, month_start.month, 1, tzinfo=timezone.utc)
    day_spent = await spent_inr(db, day=today)
    month_spent = await spent_inr(db, month_start=month_start)

    async def _counts_since(since: datetime) -> tuple[int, int, int]:
        total = (
            await db.execute(
                select(func.count()).where(
                    AiUsageLog.organization_id == ORG,
                    AiUsageLog.created_at >= since,
                )
            )
        ).scalar_one()
        ok = (
            await db.execute(
                select(func.count()).where(
                    AiUsageLog.organization_id == ORG,
                    AiUsageLog.created_at >= since,
                    AiUsageLog.success.is_(True),
                )
            )
        ).scalar_one()
        failed = (
            await db.execute(
                select(func.count()).where(
                    AiUsageLog.organization_id == ORG,
                    AiUsageLog.created_at >= since,
                    AiUsageLog.success.is_(False),
                )
            )
        ).scalar_one()
        return int(total or 0), int(ok or 0), int(failed or 0)

    async def _tokens_since(since: datetime) -> tuple[int, int, int, int]:
        tok = (
            await db.execute(
                select(
                    func.coalesce(func.sum(AiUsageLog.input_tokens), 0),
                    func.coalesce(func.sum(AiUsageLog.output_tokens), 0),
                    func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                    func.coalesce(func.sum(AiUsageLog.thoughts_tokens), 0),
                ).where(
                    AiUsageLog.organization_id == ORG,
                    AiUsageLog.created_at >= since,
                )
            )
        ).one()
        input_n = int(tok[0] or 0)
        output_n = int(tok[1] or 0)
        total_n = int(tok[2] or 0)
        thoughts_n = int(tok[3] or 0)
        if total_n <= 0:
            total_n = input_n + output_n + thoughts_n
        return input_n, output_n, total_n, thoughts_n

    req_today, ok_today, failed_today = await _counts_since(day_start)
    req_month, ok_month, failed_month = await _counts_since(month_start_dt)
    input_today, output_today, total_today, thoughts_today = await _tokens_since(day_start)
    input_month, output_month, total_month, thoughts_month = await _tokens_since(month_start_dt)

    async def _bucket_stats(since: datetime) -> dict[str, dict[str, float | int]]:
        rows = (
            await db.execute(
                select(
                    AiUsageLog.purpose,
                    func.count(),
                    func.coalesce(func.sum(AiUsageLog.estimated_cost_inr), 0),
                    func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                    func.coalesce(func.sum(AiUsageLog.input_tokens), 0),
                    func.coalesce(func.sum(AiUsageLog.output_tokens), 0),
                ).where(
                    AiUsageLog.organization_id == ORG,
                    AiUsageLog.created_at >= since,
                ).group_by(AiUsageLog.purpose)
            )
        ).all()
        out: dict[str, dict[str, float | int]] = {
            "whatsapp": {"requests": 0, "spend_inr": 0.0, "total_tokens": 0},
            "agent": {"requests": 0, "spend_inr": 0.0, "total_tokens": 0},
            "other": {"requests": 0, "spend_inr": 0.0, "total_tokens": 0},
            "intel": {"requests": 0, "spend_inr": 0.0, "total_tokens": 0},
        }
        for purpose, cnt, spend, tot, inn, outn in rows:
            bucket = purpose_bucket(str(purpose or ""))
            slot = out[bucket]
            tokens = int(tot or 0)
            if tokens <= 0:
                tokens = int(inn or 0) + int(outn or 0)
            slot["requests"] = int(slot["requests"]) + int(cnt or 0)
            slot["spend_inr"] = float(slot["spend_inr"]) + float(spend or 0)
            slot["total_tokens"] = int(slot["total_tokens"]) + tokens
        for slot in out.values():
            slot["spend_inr"] = round(float(slot["spend_inr"]), 6)
        return out

    by_source_today = await _bucket_stats(day_start)
    by_source_month = await _bucket_stats(month_start_dt)

    limit = max(1, min(int(recent_limit or 50), 200))
    recent_rows = (
        await db.execute(
            select(AiUsageLog)
            .where(AiUsageLog.organization_id == ORG)
            .order_by(AiUsageLog.id.desc())
            .limit(limit)
        )
    ).scalars().all()

    emp_ids = {r.employee_id for r in recent_rows if r.employee_id}
    user_ids = {r.user_id for r in recent_rows if r.user_id}
    emp_names: dict[int, str] = {}
    user_names: dict[int, str] = {}
    if emp_ids:
        for eid, ename in (
            await db.execute(select(Employee.id, Employee.name).where(Employee.id.in_(emp_ids)))
        ).all():
            emp_names[int(eid)] = (ename or "").strip()
    if user_ids:
        for uid, uname in (
            await db.execute(select(User.id, User.name).where(User.id.in_(user_ids)))
        ).all():
            user_names[int(uid)] = (uname or "").strip()

    recent = []
    for r in recent_rows:
        emp_name = emp_names.get(int(r.employee_id)) if r.employee_id else ""
        user_name = user_names.get(int(r.user_id)) if r.user_id else ""
        bucket = purpose_bucket(r.purpose or "")
        # WhatsApp features → conversation employee; Agent → acting CRM user.
        if bucket == "whatsapp":
            display_name = emp_name or user_name or ""
        elif bucket == "agent":
            display_name = user_name or emp_name or ""
        else:
            display_name = emp_name or user_name or ""
        total_tokens = int(getattr(r, "total_tokens", 0) or 0)
        if total_tokens <= 0:
            total_tokens = int(r.input_tokens or 0) + int(r.output_tokens or 0) + int(
                getattr(r, "thoughts_tokens", 0) or 0
            )
        recent.append(
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "purpose": r.purpose or "",
                "source": bucket,
                "provider": r.provider or "",
                "model": r.model or "",
                "input_tokens": int(r.input_tokens or 0),
                "output_tokens": int(r.output_tokens or 0),
                "total_tokens": total_tokens,
                "thoughts_tokens": int(getattr(r, "thoughts_tokens", 0) or 0),
                "estimated_cost_inr": float(r.estimated_cost_inr or 0),
                "duration_ms": int(r.duration_ms or 0),
                "success": bool(r.success),
                "employee_id": r.employee_id,
                "employee_name": emp_name or None,
                "user_id": r.user_id,
                "user_name": user_name or None,
                "display_name": display_name or None,
                "conversation_id": getattr(r, "conversation_id", None),
                "error": (r.error or "")[:200] or None,
            }
        )

    def _rate(ok: int, total: int) -> float:
        if total <= 0:
            return 100.0
        return round(100.0 * ok / total, 1)

    monthly = Decimal(str(settings_row.monthly_budget_inr))
    return {
        "spent_today_inr": float(day_spent),
        "spent_month_inr": float(month_spent),
        "daily_budget_inr": float(settings_row.daily_budget_inr),
        "monthly_budget_inr": float(monthly),
        "remaining_monthly_inr": float(max(Decimal("0"), monthly - month_spent)),
        "requests_today": req_today,
        "requests_month": req_month,
        "success_today": ok_today,
        "failed_today": failed_today,
        "success_month": ok_month,
        "failed_month": failed_month,
        "success_rate_today": _rate(ok_today, req_today),
        "success_rate_month": _rate(ok_month, req_month),
        "input_tokens_today": input_today,
        "output_tokens_today": output_today,
        "total_tokens_today": total_today,
        "thoughts_tokens_today": thoughts_today,
        "input_tokens_month": input_month,
        "output_tokens_month": output_month,
        "total_tokens_month": total_month,
        "thoughts_tokens_month": thoughts_month,
        "by_source_today": by_source_today,
        "by_source_month": by_source_month,
        "recent": recent,
    }
