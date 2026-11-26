"""Short TTL caches for Intelligence page hot paths (tile counts + lists)."""
from __future__ import annotations

import copy
import time
from typing import Any

_FUNNEL_TTL_S = 25.0
_BLAST_TTL_S = 45.0
_ORDER_TTL_S = 25.0
_ENQUIRY_TTL_S = 25.0
_QUEUE_TTL_S = 25.0
_STAMP_MSG_TTL_S = 45.0

_funnel_by_key: dict[str, tuple[float, Any]] = {}
_blast: tuple[float, set[int]] | None = None
_order_cycles: tuple[float, Any] | None = None
_enquiry: tuple[float, tuple[list[str], dict[str, str]]] | None = None
_service_by_key: dict[str, tuple[float, Any]] = {}
_stamping_by_key: dict[str, tuple[float, Any]] = {}
_stamp_msg_ids: tuple[float, set[int]] | None = None


def invalidate_intel_list_caches() -> None:
    """Drop list/count caches after a Move-to or similar mutation."""
    global _funnel_by_key, _blast, _order_cycles, _enquiry, _service_by_key, _stamping_by_key, _stamp_msg_ids
    _funnel_by_key = {}
    _blast = None
    _order_cycles = None
    _enquiry = None
    _service_by_key = {}
    _stamping_by_key = {}
    _stamp_msg_ids = None


def get_funnel_cache(key: str) -> Any | None:
    hit = _funnel_by_key.get(key)
    if hit is None:
        return None
    ts, payload = hit
    if time.monotonic() - ts > _FUNNEL_TTL_S:
        return None
    return copy.deepcopy(payload)


def set_funnel_cache(key: str, payload: Any) -> None:
    _funnel_by_key[key] = (time.monotonic(), copy.deepcopy(payload))


def get_blast_cache() -> set[int] | None:
    global _blast
    if _blast is None:
        return None
    ts, ids = _blast
    if time.monotonic() - ts > _BLAST_TTL_S:
        return None
    return set(ids)


def set_blast_cache(ids: set[int]) -> None:
    global _blast
    _blast = (time.monotonic(), set(ids))


def get_order_cycles_cache() -> Any | None:
    if _order_cycles is None:
        return None
    ts, payload = _order_cycles
    if time.monotonic() - ts > _ORDER_TTL_S:
        return None
    return payload


def set_order_cycles_cache(payload: Any) -> None:
    global _order_cycles
    _order_cycles = (time.monotonic(), payload)


def get_enquiry_cache() -> tuple[list[str], dict[str, str]] | None:
    if _enquiry is None:
        return None
    ts, payload = _enquiry
    if time.monotonic() - ts > _ENQUIRY_TTL_S:
        return None
    ids, summaries = payload
    return list(ids), dict(summaries)


def set_enquiry_cache(ids: list[str], summaries: dict[str, str]) -> None:
    global _enquiry
    _enquiry = (time.monotonic(), (list(ids), dict(summaries)))


def get_queue_cache(kind: str, key: str) -> Any | None:
    store = _service_by_key if kind == "service" else _stamping_by_key
    hit = store.get(key)
    if hit is None:
        return None
    ts, payload = hit
    if time.monotonic() - ts > _QUEUE_TTL_S:
        return None
    return copy.deepcopy(payload)


def set_queue_cache(kind: str, key: str, payload: Any) -> None:
    store = _service_by_key if kind == "service" else _stamping_by_key
    store[key] = (time.monotonic(), copy.deepcopy(payload))


def get_stamp_msg_ids_cache() -> set[int] | None:
    if _stamp_msg_ids is None:
        return None
    ts, ids = _stamp_msg_ids
    if time.monotonic() - ts > _STAMP_MSG_TTL_S:
        return None
    return set(ids)


def set_stamp_msg_ids_cache(ids: set[int]) -> None:
    global _stamp_msg_ids
    _stamp_msg_ids = (time.monotonic(), set(ids))
