"""Backfill Gemini structured AI summaries for Care (meta_cloud) conversations."""
from __future__ import annotations

import argparse
import asyncio
import json

from app.whatsapp_outline import backfill_care_outlines


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Care WhatsApp AI outlines from CRM DB")
    parser.add_argument("--sleep", type=float, default=1.5, help="Seconds between Gemini calls")
    parser.add_argument("--limit", type=int, default=0, help="Max conversations (0 = all)")
    parser.add_argument("--force", action="store_true", help="Regenerate even if outline exists")
    parser.add_argument(
        "--all-stale",
        action="store_true",
        help="Include conversations that already have a structured outline (fingerprint may still skip)",
    )
    args = parser.parse_args()
    result = await backfill_care_outlines(
        sleep_s=max(0.0, args.sleep),
        limit=max(0, args.limit),
        force=bool(args.force),
        only_missing=not bool(args.all_stale),
    )
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    asyncio.run(main())
