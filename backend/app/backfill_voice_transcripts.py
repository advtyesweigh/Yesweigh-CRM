"""Backfill Sarvam STT for existing WhatsApp voice notes (Fahim / Surya / etc.)."""
from __future__ import annotations

import argparse
import asyncio

from app.sarvam_transcribe import backfill_voice_transcripts


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="", help="Customer phone digits filter")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = await backfill_voice_transcripts(
        customer_phone_digits=args.phone,
        limit=args.limit,
        force=args.force,
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
