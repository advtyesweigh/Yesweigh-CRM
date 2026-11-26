#!/usr/bin/env python3
"""Ops: re-apply Dealer Finder list exclusions and patch stored AI chat tool_results."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AiAgentRun, AiToolCall, DealerSearchJob, User
from app.ai.tools import tool_filter_dealer_finder_results


def _parse_result(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _job_state(result: dict[str, Any]) -> str:
    job = result.get("job")
    if isinstance(job, dict):
        return str(job.get("state") or "").strip().casefold()
    return str(result.get("state") or "").strip().casefold()


async def _find_user(db, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()


async def _find_tool_call(
    db,
    *,
    user_id: int,
    state: str,
    public_id: str | None,
) -> AiToolCall | None:
    state_cf = state.casefold()
    q = (
        select(AiToolCall)
        .join(AiAgentRun, AiAgentRun.id == AiToolCall.run_id)
        .where(
            AiAgentRun.user_id == user_id,
            AiToolCall.tool_name.in_(
                (
                    "start_dealer_finder",
                    "get_dealer_finder_results",
                    "filter_dealer_finder_results",
                )
            ),
            AiToolCall.success.is_(True),
        )
        .order_by(AiToolCall.id.desc())
        .limit(80)
    )
    rows = (await db.execute(q)).scalars().all()
    best: AiToolCall | None = None
    best_n = -1
    for call in rows:
        result = _parse_result(call.result_json)
        if not result or not result.get("results_ready"):
            continue
        if public_id:
            pid = str(
                result.get("public_id")
                or (result.get("job") or {}).get("public_id")
                or ""
            )
            if pid != public_id:
                continue
        elif _job_state(result) != state_cf:
            continue
        n = len(result.get("items") or [])
        if n > best_n:
            best = call
            best_n = n
    if best:
        return best
    # Fall back: latest matching tool on the same run as the best job card.
    for call in rows:
        result = _parse_result(call.result_json)
        if not result or not result.get("results_ready"):
            continue
        if public_id:
            pid = str(
                result.get("public_id")
                or (result.get("job") or {}).get("public_id")
                or ""
            )
            if pid == public_id:
                return call
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="fak.mzn@gmail.com")
    parser.add_argument("--state", default="Puducherry")
    parser.add_argument("--public-id", default="")
    parser.add_argument(
        "--exclude-district",
        action="append",
        dest="exclude_districts",
        default=["Mahe"],
    )
    args = parser.parse_args()

    async with SessionLocal() as db:
        user = await _find_user(db, args.email)
        if not user:
            print(f"User not found: {args.email}", file=sys.stderr)
            return 1

        public_id = (args.public_id or "").strip() or None
        if not public_id:
            jobs = (
                await db.execute(
                    select(DealerSearchJob)
                    .where(DealerSearchJob.state.ilike(args.state))
                    .where(DealerSearchJob.status == "Completed")
                    .order_by(
                        DealerSearchJob.places_found.desc().nullslast(),
                        DealerSearchJob.finished_at.desc().nullslast(),
                        DealerSearchJob.id.desc(),
                    )
                    .limit(5)
                )
            ).scalars().all()
            job = next((j for j in jobs if int(j.places_found or 0) > 0), None)
            if job:
                public_id = job.public_id
        if not public_id:
            print("No completed Dealer Finder job found", file=sys.stderr)
            return 1

        tool_call = await _find_tool_call(
            db, user_id=user.id, state=args.state, public_id=public_id
        )
        if not tool_call:
            print("No dealer finder results tool_call to patch", file=sys.stderr)
            return 1

        filter_args = {
            "public_id": public_id,
            "exclude_districts": args.exclude_districts,
        }
        filtered = await tool_filter_dealer_finder_results(db, user, filter_args)
        if not filtered.get("ok") and not filtered.get("results_ready"):
            print(f"Filter failed: {filtered.get('error')}", file=sys.stderr)
            return 1

        removed = int(filtered.get("rows_excluded") or len(filtered.get("excluded_rows_preview") or []))
        phone_n = int(filtered.get("filtered_total") or filtered.get("eligible_with_phone") or 0)

        tool_call.result_json = json.dumps(filtered, ensure_ascii=False, default=str)
        tool_call.args_json = json.dumps(filter_args, default=str)
        tool_call.tool_name = "filter_dealer_finder_results"

        run = (
            await db.execute(select(AiAgentRun).where(AiAgentRun.id == tool_call.run_id))
        ).scalar_one_or_none()
        if run:
            from app.ai.service import _card_short_summary

            blurb = _card_short_summary(
                [{"ok": True, "tool": "filter_dealer_finder_results", "result": filtered}]
            )
            if blurb:
                run.result_summary = blurb[:4000]

        await db.commit()
        preview = filtered.get("excluded_rows_preview") or []
        print(
            json.dumps(
                {
                    "ok": True,
                    "public_id": public_id,
                    "state": args.state,
                    "eligible_with_phone": phone_n,
                    "rows_removed": removed,
                    "tool_call_id": tool_call.id,
                    "run_id": tool_call.run_id,
                    "excluded_preview": preview[:5],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))