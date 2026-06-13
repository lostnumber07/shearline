"""In-memory TTL cache for upstream fetches.

Every upstream request goes through this cache so repeat tool calls within a
product's freshness window never re-hit NOAA servers (invariant 5). Expired
entries are evicted on access and on every insert, so a long-running server
does not pin stale multi-MB payloads (NEXRAD volumes, RAP subsets, rotating
MRMS sample keys) indefinitely.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

# Per-request cache hit/miss counters for HTTP observability. A request scope
# (the @observe wrapper) sets a fresh dict; get_or_fetch increments it. Using a
# ContextVar keeps counts correct under concurrent requests and propagates into
# the child tasks of a fan-out tool (e.g. the threat brief).
_metrics: ContextVar[dict[str, int] | None] = ContextVar("cache_metrics", default=None)


def start_metrics() -> dict[str, int]:
    m = {"hits": 0, "misses": 0}
    _metrics.set(m)
    return m


def _record(hit: bool) -> None:
    m = _metrics.get()
    if m is not None:
        m["hits" if hit else "misses"] += 1

# TTLs in seconds (invariant 5)
TTL_ALERTS = 60
TTL_MRMS = 120
TTL_LSR = 300
TTL_OUTLOOK = 1800
TTL_RAP = 1800
# Past storm reports are effectively immutable (rare late corrections), so a
# historical-window query can be cached far longer than the live LSR feed.
TTL_HISTORICAL = 21600  # 6 hours


class TTLCache:
    """Async-safe cache with per-key locks so concurrent callers of the same
    key trigger exactly one upstream fetch (no thundering herd)."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks.setdefault(key, asyncio.Lock())
        return lock

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._entries.items() if exp <= now]:
            self._entries.pop(key, None)
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                self._locks.pop(key, None)

    async def get_or_fetch(
        self, key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]
    ) -> Any:
        hit = self._entries.get(key)
        if hit is not None:
            if hit[0] > time.monotonic():
                _record(True)
                return hit[1]
            self._entries.pop(key, None)
        async with self._lock_for(key):
            hit = self._entries.get(key)
            if hit is not None and hit[0] > time.monotonic():
                _record(True)
                return hit[1]
            _record(False)
            value = await fetch()
            self._entries[key] = (time.monotonic() + ttl, value)
            self._sweep()
            return value

    def put(self, key: str, ttl: float, value: Any) -> None:
        self._entries[key] = (time.monotonic() + ttl, value)
        self._sweep()

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()


CACHE = TTLCache()
