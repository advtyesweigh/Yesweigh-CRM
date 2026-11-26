"""Translate recent untranslated WhatsApp text/captions to Malayalam (ml-IN).

Uses Sarvam /translate with Gemini fallback — same path as live text translation.

  PYTHONPATH=/app python -m app.backfill_text_translations --dry-run
  PYTHONPATH=/app python -m app.backfill_text_translations --limit 100
  PYTHONPATH=/app python -m app.backfill_text_translations --phone 9843371834
"""
from __future__ import annotations

import argparse
import asyncio
import json

from app.sarvam_transcribe import backfill_text_translations


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="", help="Optional customer phone digits filter")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = await backfill_text_translations(
        customer_phone_digits=args.phone,
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
