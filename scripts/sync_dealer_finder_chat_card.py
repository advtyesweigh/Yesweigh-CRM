#!/usr/bin/env python3
"""Ops: refresh stored AI chat Dealer Finder card from a live job (incl. partial while Running)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parent.parent
for _base in (_root / "backend", _root, _root.parent):
    if (_base / "app").is_dir():
        sys.path.insert(0, str(_base))
        break

from sqlalchemy import desc, select

from app.ai.service import _card_short_summary
from app.ai.tools import _dealer_finder_results_payload
from app.db import SessionLocal
from app.dealer_finder.jobs import (
    DISPLAY_ELIGIBLE_PHONE_LIMIT,
    build_job_status_report,
    job_public,
    job_results_ready,
    job_scan_complete,
    list_eligible_phone_places,
    place_counts_for_job,
    place_row_public,
)
from app.models import AiAgentRun, AiToolCall, DealerSearchJob, User


async def _find_user(db, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()


async def _resolve_run(
    db,
    *,
    user_id: int,
    run_id: int | None,
    command_substr: str,
) -> AiAgentRun | None:
    if run_id:
        return await db.get(AiAgentRun, run_id)
    needle = (command_substr or "").strip()
    if not needle:
        return None
    q = (
        select(AiAgentRun)
        .where(
            AiAgentRun.user_id == user_id,
            AiAgentRun.command.ilike(f"%{needle}%"),
        )
        .order_by(desc(AiAgentRun.id))
        .limit(1)
    )
    return (await db.execute(q)).scalar_one_or_none()


async def _patch_or_insert_tool_call(
    db,
    *,
    run_id: int,
    payload: dict[str, Any],
) -> AiToolCall:
    rows = (
        await db.execute(
            select(AiToolCall)
            .where(
                AiToolCall.run_id == run_id,
                AiToolCall.tool_name.in_(
                    (
                        "get_dealer_finder_results",
                        "start_dealer_finder",
                        "get_dealer_finder_job",
                        "filter_dealer_finder_results",
                    )
                ),
            )
            .order_by(AiToolCall.id.desc())
        )
    ).scalars().all()
    call = next(
        (c for c in rows if c.tool_name == "get_dealer_finder_results"),
        rows[0] if rows else None,
    )
    args = {
        "public_id": payload.get("public_id")
        or (payload.get("job") or {}).get("public_id"),
        "mobile_only": True,
    }
    if call:
        call.tool_name = "filter_dealer_finder_results"
        call.result_json = json.dumps(payload, ensure_ascii=False, default=str)
        call.args_json = json.dumps(args, default=str)
        call.success = True
        call.error = ""
        return call
    call = AiToolCall(
        run_id=run_id,
        tool_name="filter_dealer_finder_results",
        args_json=json.dumps(args, default=str),
        result_json=json.dumps(payload, ensure_ascii=False, default=str),
        success=True,
        error="",
        duration_ms=0,
    )
    db.add(call)
    await db.flush()
    return call


async def build_chat_payload(db, job: DealerSearchJob) -> dict[str, Any]:
    report = await build_job_status_report(db, job)
    counts = await place_counts_for_job(db, job.id)
    discovery = job_scan_complete(job) and job_results_ready(job)
    rows, filtered_total = await list_eligible_phone_places(
        db,
        job.id,
        limit=DISPLAY_ELIGIBLE_PHONE_LIMIT,
        mobile_only=True,
        discovery=discovery,
    )
    if discovery:
        filtered_total = int(counts.get("discovery_mobile_new") or filtered_total)
    items = [place_row_public(r) for r in rows]
    pub = job_public(job)
    incomplete = not job_scan_complete(job)
    importable = int(counts.get("importable_with_phone") or counts.get("eligible_with_phone") or 0)
    return _dealer_finder_results_payload(
        job=job,
        pub=pub,
        report=report,
        counts=counts,
        items=items,
        filtered_total=filtered_total,
        incomplete=incomplete,
        waited=False,
        importable_phone_count=importable if discovery else None,
        mobile_only=True,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="fak.mzn@gmail.com")
    parser.add_argument("--public-id", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument(
        "--command-substr",
        default="Kerala",
        help="Find latest run when --run-id omitted",
    )
    args = parser.parse_args()
    public_id = args.public_id.strip()

    async with SessionLocal() as db:
        user = await _find_user(db, args.email)
        if not user:
            print(f"User not found: {args.email}", file=sys.stderr)
            return 1

        job = (
            await db.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == public_id)
            )
        ).scalar_one_or_none()
        if not job:
            print(f"Job not found: {public_id}", file=sys.stderr)
            return 1

        run = await _resolve_run(
            db,
            user_id=user.id,
            run_id=args.run_id or None,
            command_substr=args.command_substr,
        )
        if not run:
            print("No matching AI run to patch", file=sys.stderr)
            return 1

        payload = await build_chat_payload(db, job)
        call = await _patch_or_insert_tool_call(db, run_id=run.id, payload=payload)
        blurb = _card_short_summary(
            [{"ok": True, "tool": "get_dealer_finder_results", "result": payload}]
        )
        if blurb:
            run.result_summary = blurb[:4000]
        if (run.status or "").lower() in {"", "queued", "running"}:
            run.status = "completed"

        await db.commit()
        job_pub = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        print(
            json.dumps(
                {
                    "ok": True,
                    "run_id": run.id,
                    "tool_call_id": call.id,
                    "public_id": public_id,
                    "job_status": job_pub.get("status"),
                    "units": f"{job_pub.get('completed_units')}/{job_pub.get('total_units')}",
                    "eligible_with_phone": payload.get("eligible_with_phone"),
                    "items_on_card": len(payload.get("items") or []),
                    "incomplete": payload.get("incomplete"),
                    "result_summary": (run.result_summary or "")[:220],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))