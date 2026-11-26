"""Backfill inbound voice spoken_language via Sarvam STT + sync conversation.language.

Does not use translated transcript script — only Sarvam language_code from audio.

  PYTHONPATH=/app python -m app.backfill_voice_spoken_language --dry-run
  PYTHONPATH=/app python -m app.backfill_voice_spoken_language --limit 100 --after-id 0
  PYTHONPATH=/app python -m app.backfill_voice_spoken_language --limit 50 --sleep 1.5
  PYTHONPATH=/app python -m app.backfill_voice_spoken_language --reconcile-only
"""
from __future__ import annotations

import argparse
import asyncio
import json

from app.contact_spoken_language import reconcile_conversation_languages_from_voice
from app.db import SessionLocal
from app.sarvam_transcribe import backfill_voice_spoken_language


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="", help="Optional customer phone digits filter")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--after-id",
        type=int,
        default=0,
        help="Resume: only message ids greater than this (ascending batch)",
    )
    parser.add_argument("--force", action="store_true", help="Re-STT even when spoken_language set")
    parser.add_argument(
        "--all-voices",
        action="store_true",
        help="Process every inbound voice (same as --force for backlog selection)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=1.25, help="Seconds between Sarvam STT calls")
    parser.add_argument(
        "--no-reconcile",
        action="store_true",
        help="Skip DB-only conversation.language sync after STT batch",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Only sync conversation.language from stored spoken_language (no STT)",
    )
    args = parser.parse_args()

    if args.reconcile_only:
        async with SessionLocal() as db:
            result = await reconcile_conversation_languages_from_voice(db)
        print(json.dumps({"ok": True, "reconcile_only": True, **result}, ensure_ascii=False, indent=2))
        return

    result = await backfill_voice_spoken_language(
        customer_phone_digits=args.phone,
        limit=args.limit,
        after_id=args.after_id,
        force=args.force,
        all_voices=args.all_voices,
        dry_run=args.dry_run,
        sleep_s=max(0.0, args.sleep),
        reconcile=not args.no_reconcile,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())