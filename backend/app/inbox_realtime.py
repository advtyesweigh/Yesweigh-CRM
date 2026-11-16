"""In-process pub/sub for live WhatsApp inbox notifications (SSE)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

_subscribers: set[asyncio.Queue[str]] = set()
_lock = asyncio.Lock()


async def subscribe() -> asyncio.Queue[str]:
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
    async with _lock:
        _subscribers.add(q)
    return q


async def unsubscribe(q: asyncio.Queue[str]) -> None:
    async with _lock:
        _subscribers.discard(q)


async def publish(event: dict[str, Any]) -> None:
    payload = json.dumps(event, default=str)
    async with _lock:
        dead: list[asyncio.Queue[str]] = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                    q.put_nowait(payload)
                except Exception:  # noqa: BLE001
                    dead.append(q)
            except Exception:  # noqa: BLE001
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)
