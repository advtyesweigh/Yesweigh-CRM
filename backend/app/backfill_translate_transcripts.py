"""Translate stored voice transcripts to configured target language (default ml-IN).

Uses Sarvam /translate only — no audio re-STT.

  PYTHONPATH=/app python -m app.backfill_translate_transcripts --dry-run
  PYTHONPATH=/app python -m app.backfill_translate_transcripts --limit 500
  PYTHONPATH=/app python -m app.backfill_translate_transcripts --phone 9843371834
"""
from __future__ import annotations

import argparse
import asyncio
import json

from app.sarvam_transcribe import backfill_translate_stored_transcripts


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="", help="Optional customer phone digits filter")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = await backfill_translate_stored_transcripts(
        customer_phone_digits=args.phone,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
