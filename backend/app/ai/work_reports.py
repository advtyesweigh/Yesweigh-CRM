"""AI Agent Work Reports — task log for /work → Work Reports tab."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiAgentRun,
    AiToolCall,
    AiWorkReport,
    Employee,
    User,
)

ORG = "yesweigh"

# Tools that produce a Work Reports row (sends, searches, reports).
SIGNIFICANT_TOOLS: frozenset[str] = frozenset(
    {
        "send_whatsapp_text",
        "send_whatsapp_text_batch",
        "send_whatsapp_media_batch",
        "list_campaign_media",
        "report_enquiries",
        "get_sales_summary",
        "get_dealer_sales",
        "get_region_sales",
        "get_product_sales_by_dealer",
        "search_dealers",
        "search_leads",
        "search_google_businesses",
        "find_new_dealers",
        "start_dealer_finder",
        "import_dealer_finder_leads",
        "query_business_data",
        "search_whatsapp_messages",
        "search_vendors",
        "list_whatsapp_threads",
        "get_whatsapp_thread",
        "get_unanswered_messages",
        "request_new_tool",
    }
)

_TASK_LABELS: dict[str, str] = {
    "send_whatsapp_text": "Send WhatsApp message",
    "send_whatsapp_text_batch": "Send WhatsApp batch",
    "send_whatsapp_media_batch": "Send WhatsApp media batch",
    "list_campaign_media": "List campaign media",
    "report_enquiries": "Enquiry report",
    "get_sales_summary": "Sales summary",
    "get_dealer_sales": "Dealer sales",
    "get_region_sales": "Region sales",
    "get_product_sales_by_dealer": "Product sales by dealer",
    "search_dealers": "Search dealers",
    "search_leads": "Search leads",
    "search_google_businesses": "Google business search",
    "find_new_dealers": "Find new dealers (Google − CRM)",
    "start_dealer_finder": "Dealer Finder batch (district→city)",
    "import_dealer_finder_leads": "Import Dealer Finder leads",
    "query_business_data": "Query business data",
    "search_whatsapp_messages": "Search WhatsApp messages",
    "search_vendors": "Search vendors",
    "list_whatsapp_threads": "List WhatsApp threads",
    "get_whatsapp_thread": "WhatsApp thread",
    "get_unanswered_messages": "Unanswered messages",
    "request_new_tool": "Request new tool",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def task_label(tool_name: str) -> str:
    return _TASK_LABELS.get(tool_name) or tool_name.replace("_", " ").strip().title()


def _clip(text: str, limit: int = 800) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def _phone_display(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 10:
        return f"+91 {digits[:5]} {digits[5:]}"
    if digits.startswith("91") and len(digits) == 12:
        local = digits[2:]
        return f"+91 {local[:5]} {local[5:]}"
    return phone or ""


def _confirm_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "confirm", "confirmed"}


def _should_skip_tool(tool_name: str, args: dict[str, Any], result: dict[str, Any] | None) -> bool:
    """Skip previews / confirmation prompts — only real work belongs in reports."""
    if tool_name not in SIGNIFICANT_TOOLS:
        return True
    if tool_name in {
        "send_whatsapp_text",
        "send_whatsapp_text_batch",
        "send_whatsapp_media_batch",
    }:
        # Only log actual send attempts (confirm_send + not dry_run), or hard send failures.
        confirm = _confirm_truthy(args.get("confirm_send"))
        dry = _confirm_truthy(args.get("dry_run"))
        if dry:
            return True
        if isinstance(result, dict):
            if result.get("dry_run") or result.get("needs_confirmation"):
                return True
            if result.get("sent") is True or int(result.get("sent_count") or 0) > 0:
                return False
            # Paced schedule accepted — log as running campaign
            if result.get("paced") or result.get("scheduled"):
                return False
            err = str(result.get("error") or "")
            if err.startswith("Send failed"):
                return False
            # Real confirm attempt that failed (instance missing, opted out, etc.)
            if confirm and result.get("ok") is False:
                return False
            if confirm and tool_name in {
                "send_whatsapp_text_batch",
                "send_whatsapp_media_batch",
            }:
                return False
            return True
        return not confirm
    if tool_name in {"start_dealer_finder", "import_dealer_finder_leads"}:
        if isinstance(result, dict) and (
            result.get("needs_confirm") or result.get("needs_confirmation")
        ):
            return True
        if not _confirm_truthy(args.get("confirm")):
            return True
    return False


def _details_send_whatsapp(args: dict[str, Any], result: dict[str, Any] | None, error: str) -> str:
    payload = result if isinstance(result, dict) else {}
    to_phone = str(payload.get("to_phone") or args.get("to_phone") or "").strip()
    customer = str(args.get("customer_name") or payload.get("customer_name") or "").strip()
    message = str(args.get("message") or payload.get("message_preview") or "").strip()
    phone_fmt = _phone_display(to_phone)
    who = customer or phone_fmt or "recipient"
    if customer and phone_fmt:
        who = f"{customer} ({phone_fmt})"
    elif phone_fmt:
        who = phone_fmt
    parts = [f"To: {who}"]
    if message:
        parts.append(f'Message: "{_clip(message, 400)}"')
    if error:
        parts.append(f"Error: {_clip(error, 200)}")
    return " · ".join(parts)


def _details_send_whatsapp_batch(args: dict[str, Any], result: dict[str, Any] | None, error: str) -> str:
    payload = result if isinstance(result, dict) else {}
    message = str(
        args.get("message")
        or args.get("caption")
        or payload.get("message_preview")
        or payload.get("caption_preview")
        or ""
    ).strip()
    media_title = str(payload.get("media_title") or args.get("media_query") or "").strip()
    sent_n = int(payload.get("sent_count") or 0)
    fail_n = int(payload.get("failed_count") or 0)
    total = int(payload.get("recipient_count") or payload.get("attempted") or 0)
    delay = payload.get("delay_seconds")
    if delay is None:
        delay = args.get("delay_seconds")
    hours = payload.get("total_hours")
    if hours is None:
        hours = args.get("total_hours")
    if payload.get("paced") or payload.get("scheduled"):
        parts = [f"Paced campaign scheduled: 0/{total or '?'} sent so far"]
    else:
        parts = [f"Batch: {sent_n}/{total or '?'} sent"]
    if media_title:
        parts.append(f"Media: {media_title}")
    if fail_n:
        parts.append(f"failed: {fail_n}")
    if hours is not None and hours != "":
        parts.append(f"window: {hours}h")
    elif delay is not None and delay != "":
        parts.append(f"delay: {delay}s")
    if message:
        parts.append(f'Message: "{_clip(message, 280)}"')
    rows = payload.get("results") if isinstance(payload.get("results"), list) else []
    if rows:
        snippets: list[str] = []
        for row in rows[:12]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("customer_name") or "").strip()
            phone = _phone_display(str(row.get("to_phone") or ""))
            who = name or phone or "?"
            if name and phone:
                who = f"{name} ({phone})"
            flag = "sent" if row.get("sent") else ("err" if row.get("error") else "skip")
            snippets.append(f"{who}:{flag}")
        if snippets:
            parts.append("Recipients: " + "; ".join(snippets))
            if len(rows) > 12:
                parts.append(f"+{len(rows) - 12} more")
    if error:
        parts.append(f"Error: {_clip(error, 200)}")
    stop = str(payload.get("stop_reason") or "").strip()
    if stop:
        parts.append(f"Stopped: {_clip(stop, 160)}")
    return _clip(" · ".join(parts), 800)


def _details_generic(tool_name: str, args: dict[str, Any], result: dict[str, Any] | None, error: str) -> str:
    bits: list[str] = []
    for key in (
        "query",
        "dealer_name",
        "dealer_query",
        "region",
        "district",
        "product",
        "period",
        "month",
        "year",
        "customer_phone",
        "phone",
        "thread_id",
        "conversation_id",
        "limit",
    ):
        val = args.get(key)
        if val is None or val == "":
            continue
        bits.append(f"{key.replace('_', ' ')}: {val}")
    if isinstance(result, dict):
        if result.get("count") is not None:
            bits.append(f"count: {result.get('count')}")
        elif result.get("total") is not None:
            bits.append(f"total: {result.get('total')}")
        elif isinstance(result.get("dealers"), list):
            bits.append(f"dealers: {len(result['dealers'])}")
        elif isinstance(result.get("messages"), list):
            bits.append(f"messages: {len(result['messages'])}")
        elif isinstance(result.get("threads"), list):
            bits.append(f"threads: {len(result['threads'])}")
        note = str(result.get("note") or result.get("summary") or "").strip()
        if note:
            bits.append(_clip(note, 240))
    if error:
        bits.append(f"Error: {_clip(error, 200)}")
    if not bits:
        return task_label(tool_name)
    return _clip(" · ".join(str(b) for b in bits), 800)


def build_task_details(
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any] | None,
    error: str = "",
) -> str:
    if tool_name == "send_whatsapp_text":
        return _details_send_whatsapp(args, result, error)
    if tool_name in {"send_whatsapp_text_batch", "send_whatsapp_media_batch"}:
        return _details_send_whatsapp_batch(args, result, error)
    return _details_generic(tool_name, args, result, error)


def resolve_agent_name(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any] | None,
    employee: Employee | None,
    user: User | None,
) -> str:
    payload = result if isinstance(result, dict) else {}
    if tool_name in {
        "send_whatsapp_text",
        "send_whatsapp_text_batch",
        "send_whatsapp_media_batch",
    }:
        for key in ("from_employee_name", "from_display_name"):
            val = str(payload.get(key) or "").strip()
            if val:
                return val[:200]
        emp_code = str(payload.get("from_employee_code") or args.get("from_employee_code") or "").strip()
        if emp_code and employee and (employee.code or "") == emp_code and employee.name:
            return employee.name[:200]
    if employee and (employee.name or "").strip():
        return employee.name.strip()[:200]
    if user and (user.name or "").strip():
        return user.name.strip()[:200]
    return "AI Agent"


def resolve_status(*, tool_ok: bool, result: dict[str, Any] | None, run_status: str | None = None) -> str:
    if run_status == "cancelled":
        return "stopped"
    if run_status == "running":
        return "running"
    if not tool_ok:
        return "failed"
    if isinstance(result, dict):
        if result.get("ok") is False or result.get("sent") is False and "send" in str(result.get("note") or "").lower():
            # send failed after attempt
            if result.get("sent") is False and result.get("error"):
                return "failed"
        if result.get("ok") is False:
            return "failed"
        if result.get("sent") is False and result.get("error"):
            return "failed"
    return "completed"


def _parse_json_obj(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    # Truncated storage may append …
    if text.endswith("…"):
        text = text.rstrip("…").rstrip()
        # try to close truncated JSON loosely
        if not text.endswith("}"):
            text = text + '"}'
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


async def record_from_tool_call(
    db: AsyncSession,
    *,
    user: User,
    run_id: int,
    tool_call: AiToolCall,
    args: dict[str, Any],
    result: dict[str, Any] | None,
    employee: Employee | None = None,
) -> AiWorkReport | None:
    """Insert a work report for a significant tool call (idempotent on tool_call_id)."""
    name = (tool_call.tool_name or "").strip()
    if _should_skip_tool(name, args, result):
        return None

    existing = (
        await db.execute(select(AiWorkReport).where(AiWorkReport.tool_call_id == tool_call.id))
    ).scalar_one_or_none()
    if existing:
        return existing

    emp = employee
    if emp is None:
        from app.ai.tools import employee_for_user

        emp = await employee_for_user(db, user)

    run = (
        await db.execute(select(AiAgentRun).where(AiAgentRun.id == run_id))
    ).scalar_one_or_none()

    status = resolve_status(
        tool_ok=bool(tool_call.success),
        result=result,
        run_status=run.status if run else None,
    )
    # Prefer domain ok/sent for sends
    if name in {
        "send_whatsapp_text",
        "send_whatsapp_text_batch",
        "send_whatsapp_media_batch",
    } and isinstance(result, dict):
        if result.get("paced") or result.get("scheduled"):
            status = "running"
        elif result.get("sent") is True or int(result.get("sent_count") or 0) > 0:
            status = "completed" if not result.get("stopped_early") else "failed"
            if (
                name in {"send_whatsapp_text_batch", "send_whatsapp_media_batch"}
                and int(result.get("sent_count") or 0) > 0
                and (int(result.get("failed_count") or 0) > 0 or result.get("stopped_early"))
            ):
                # Partial batch success still shows as completed with failure details
                status = "completed"
        elif result.get("error") or result.get("ok") is False:
            status = "failed"

    error = (tool_call.error or "").strip()
    if not error and isinstance(result, dict):
        error = str(result.get("error") or "").strip()

    agent = resolve_agent_name(
        tool_name=name, args=args, result=result, employee=emp, user=user
    )
    occurred = tool_call.created_at or _now()
    employee_id = emp.id if emp else (run.employee_id if run else None)

    # Batch: one Work Reports row per recipient (plus summary if no per-row data)
    if name in {"send_whatsapp_text_batch", "send_whatsapp_media_batch"} and isinstance(result, dict):
        rows_out = result.get("results") if isinstance(result.get("results"), list) else []
        created_any: AiWorkReport | None = None
        message = str(
            args.get("message")
            or args.get("caption")
            or result.get("message_preview")
            or result.get("caption_preview")
            or ""
        ).strip()
        media_title = str(result.get("media_title") or "").strip()
        if media_title and not message:
            message = f"[media] {media_title}"
        elif media_title:
            message = f"{message} · media: {media_title}"
        task_name = (
            "Send WhatsApp media"
            if name == "send_whatsapp_media_batch"
            else "Send WhatsApp message"
        )
        for i, item in enumerate(rows_out):
            if not isinstance(item, dict):
                continue
            item_err = str(item.get("error") or "").strip()
            if item.get("dry_run"):
                continue
            skipped = bool(
                item.get("skipped_duplicate")
                or item.get("skipped_self")
                or item.get("skipped_staff")
            )
            if item.get("sent"):
                item_status = "completed"
            elif skipped:
                item_status = "completed"
                if not item_err:
                    item_err = str(item.get("note") or "Skipped").strip()
            elif item_err:
                item_status = "failed"
            else:
                item_status = status
            detail = _details_send_whatsapp(
                {
                    "to_phone": item.get("to_phone"),
                    "customer_name": item.get("customer_name"),
                    "message": message,
                },
                item,
                item_err,
            )
            # Append batch stop note only on the failed row that triggered it
            stop_reason = str(result.get("stop_reason") or "").strip()
            if (
                item_status == "failed"
                and result.get("stopped_early")
                and stop_reason
                and stop_reason in item_err
            ):
                detail = f"{detail} · Batch stopped early (instance/gateway fatal)"
            row = AiWorkReport(
                public_id=str(uuid.uuid4()),
                organization_id=ORG,
                run_id=run_id,
                # Unique tool_call_id: only first recipient links for idempotency
                tool_call_id=tool_call.id if i == 0 else None,
                user_id=user.id,
                employee_id=employee_id,
                tool_name=name,
                task_name=task_name[:200],
                agent_name=agent,
                task_details=detail,
                status=item_status,
                occurred_at=occurred,
            )
            db.add(row)
            created_any = created_any or row
        # Summary row when batch stopped early or had failures — non-blocking queue note
        if created_any is not None:
            failed_n = int(result.get("failed_count") or 0)
            sent_n = int(result.get("sent_count") or 0)
            stop_reason = str(result.get("stop_reason") or "").strip()
            if failed_n or result.get("stopped_early"):
                summary_bits = [
                    str(result.get("note") or "").strip()
                    or f"Batch: sent {sent_n}, failed {failed_n}",
                ]
                if result.get("stopped_early") and stop_reason:
                    summary_bits.append(f"Stopped early: {stop_reason}")
                else:
                    summary_bits.append("Continued past per-recipient failures (queue not blocked).")
                summary = AiWorkReport(
                    public_id=str(uuid.uuid4()),
                    organization_id=ORG,
                    run_id=run_id,
                    tool_call_id=None,
                    user_id=user.id,
                    employee_id=employee_id,
                    tool_name=name,
                    task_name=f"{task_name} (batch summary)"[:200],
                    agent_name=agent,
                    task_details=" · ".join(b for b in summary_bits if b)[:4000],
                    status="completed" if sent_n > 0 and not result.get("stopped_early") else (
                        "failed" if sent_n == 0 else "completed"
                    ),
                    occurred_at=occurred,
                )
                db.add(summary)
            await db.flush()
            return created_any
        # Fall through to single summary row when results empty

    row = AiWorkReport(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        run_id=run_id,
        tool_call_id=tool_call.id,
        user_id=user.id,
        employee_id=employee_id,
        tool_name=name,
        task_name=task_label(name)[:200],
        agent_name=agent,
        task_details=build_task_details(name, args, result, error),
        status=status,
        occurred_at=occurred,
    )
    db.add(row)
    await db.flush()
    return row


async def mark_run_reports_stopped(db: AsyncSession, run_id: int) -> None:
    rows = (
        await db.execute(
            select(AiWorkReport).where(
                AiWorkReport.run_id == run_id,
                AiWorkReport.status == "running",
            )
        )
    ).scalars().all()
    for row in rows:
        row.status = "stopped"
    if rows:
        await db.flush()


async def backfill_from_tool_calls(db: AsyncSession, *, limit: int = 500) -> int:
    """Create work reports for recent significant tool calls missing a report row."""
    existing_ids = set(
        (
            await db.execute(
                select(AiWorkReport.tool_call_id).where(AiWorkReport.tool_call_id.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    calls = (
        await db.execute(
            select(AiToolCall)
            .where(AiToolCall.tool_name.in_(list(SIGNIFICANT_TOOLS)))
            .order_by(AiToolCall.id.desc())
            .limit(limit)
        )
    ).scalars().all()

    created = 0
    # Cache users / employees
    user_cache: dict[int, User] = {}
    emp_cache: dict[int, Employee | None] = {}

    for call in reversed(calls):
        if call.id in existing_ids:
            continue
        args = _parse_json_obj(call.args_json) or {}
        result = _parse_json_obj(call.result_json) if call.success else None
        if _should_skip_tool(call.tool_name, args, result):
            continue

        run = (
            await db.execute(select(AiAgentRun).where(AiAgentRun.id == call.run_id))
        ).scalar_one_or_none()
        if not run:
            continue

        user = user_cache.get(run.user_id)
        if user is None:
            user = (
                await db.execute(select(User).where(User.id == run.user_id))
            ).scalar_one_or_none()
            if user:
                user_cache[run.user_id] = user
        if not user:
            continue

        emp = emp_cache.get(run.user_id)
        if run.user_id not in emp_cache:
            from app.ai.tools import employee_for_user

            emp = await employee_for_user(db, user)
            emp_cache[run.user_id] = emp

        row = await record_from_tool_call(
            db,
            user=user,
            run_id=call.run_id,
            tool_call=call,
            args=args,
            result=result,
            employee=emp,
        )
        if row:
            created += 1
            existing_ids.add(call.id)
    return created


def _report_dict(row: AiWorkReport, *, run_public_id: str | None = None) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "run_id": run_public_id,
        "tool_name": row.tool_name,
        "task_name": row.task_name,
        "agent_name": row.agent_name,
        "task_details": row.task_details,
        "status": row.status,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_work_reports(
    db: AsyncSession,
    user: User,
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List work reports for the org (all users — Work Reports is a shared ops log)."""
    _ = user
    limit = max(1, min(200, limit))

    # Lazy backfill once per process / when table still empty-ish
    count_existing = (
        await db.execute(select(AiWorkReport.id).where(AiWorkReport.organization_id == ORG).limit(1))
    ).scalar_one_or_none()
    if count_existing is None:
        await backfill_from_tool_calls(db, limit=500)
        await db.commit()

    q = (
        select(AiWorkReport)
        .where(AiWorkReport.organization_id == ORG)
        .order_by(AiWorkReport.occurred_at.desc(), AiWorkReport.id.desc())
        .limit(limit)
    )
    if status:
        q = q.where(AiWorkReport.status == status.strip().lower())

    rows = (await db.execute(q)).scalars().all()
    run_ids = {r.run_id for r in rows if r.run_id}
    run_public: dict[int, str] = {}
    if run_ids:
        runs = (
            await db.execute(select(AiAgentRun).where(AiAgentRun.id.in_(run_ids)))
        ).scalars().all()
        run_public = {r.id: r.public_id for r in runs}

    out = [_report_dict(r, run_public_id=run_public.get(r.run_id) if r.run_id else None) for r in rows]

    # Surface in-flight runs that have not logged a significant tool yet
    running = (
        await db.execute(
            select(AiAgentRun)
            .where(
                AiAgentRun.organization_id == ORG,
                AiAgentRun.status.in_(("queued", "running")),
            )
            .order_by(AiAgentRun.id.desc())
            .limit(20)
        )
    ).scalars().all()
    reported_run_ids = {r.run_id for r in rows if r.run_id}
    for run in running:
        if run.id in reported_run_ids:
            continue
        emp_name = ""
        if run.employee_id:
            emp = (
                await db.execute(select(Employee).where(Employee.id == run.employee_id))
            ).scalar_one_or_none()
            emp_name = (emp.name if emp else "") or ""
        if not emp_name:
            u = (
                await db.execute(select(User).where(User.id == run.user_id))
            ).scalar_one_or_none()
            emp_name = (u.name if u else "") or "AI Agent"
        cmd = (run.command or "").strip()
        out.insert(
            0,
            {
                "id": f"running-{run.public_id}",
                "run_id": run.public_id,
                "tool_name": "",
                "task_name": _clip(cmd, 120) or "AI Agent task",
                "agent_name": emp_name[:200],
                "task_details": _clip(cmd, 400) or "In progress…",
                "status": "running",
                "occurred_at": (run.started_at or run.created_at).isoformat()
                if (run.started_at or run.created_at)
                else None,
                "created_at": run.created_at.isoformat() if run.created_at else None,
            },
        )
    return out[:limit]
