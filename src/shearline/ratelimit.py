"""Per-client token-bucket rate limiting for the HTTP transport.

A pure-ASGI wrapper (not Starlette BaseHTTPMiddleware, which buffers and would
interfere with the streamable-HTTP transport). It either passes the request
through untouched or short-circuits with a 429 + Retry-After + a clear JSON
error body. Keyed by the first X-Forwarded-For IP if present (for deployments
behind a proxy), else the direct client IP.

Defaults (overridable by env):
  SHEARLINE_RATE_RPM    sustained requests/minute/client   (default 60; 0 = off)
  SHEARLINE_RATE_BURST  bucket capacity / max burst        (default 30)
"""

import json
import os
import time
from typing import Any

from .envelope import DISCLAIMER

DEFAULT_RPM = 60
DEFAULT_BURST = 30
_MAX_BUCKETS = 10000  # bound memory; prune stale entries past this


class RateLimitMiddleware:
    def __init__(self, app: Any, rpm: float = DEFAULT_RPM, burst: float = DEFAULT_BURST):
        self.app = app
        self.rate = rpm / 60.0  # tokens per second
        self.burst = float(burst)
        self.enabled = rpm > 0
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_seen)

    @classmethod
    def from_env(cls, app: Any) -> Any:
        rpm = float(os.environ.get("SHEARLINE_RATE_RPM", DEFAULT_RPM))
        burst = float(os.environ.get("SHEARLINE_RATE_BURST", DEFAULT_BURST))
        mw = cls(app, rpm=rpm, burst=burst)
        return mw if mw.enabled else app  # transparently skip when disabled

    def _key(self, scope: dict) -> str:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _allow(self, key: str, now: float) -> tuple[bool, float]:
        tokens, last_seen = self._buckets.get(key, (self.burst, now))
        # Refill since we last saw this client.
        tokens = min(self.burst, tokens + (now - last_seen) * self.rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0
        self._buckets[key] = (tokens, now)
        retry = (1.0 - tokens) / self.rate if self.rate else 60.0
        return False, retry

    def _prune(self, now: float) -> None:
        if len(self._buckets) <= _MAX_BUCKETS:
            return
        # Drop entries idle for over a minute; if still large, clear oldest.
        stale = [k for k, (_, last) in self._buckets.items() if now - last > 60]
        for k in stale:
            self._buckets.pop(k, None)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        now = time.monotonic()
        self._prune(now)
        allowed, retry = self._allow(self._key(scope), now)
        if allowed:
            return await self.app(scope, receive, send)

        retry_s = max(1, round(retry))
        body = json.dumps(
            {
                "error": "rate_limited",
                "message": (
                    f"Rate limit exceeded; retry in ~{retry_s}s. "
                    "Set SHEARLINE_RATE_RPM to adjust."
                ),
                "retry_after_seconds": retry_s,
                "disclaimer": DISCLAIMER,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(retry_s).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
