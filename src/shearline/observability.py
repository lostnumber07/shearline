"""Structured per-request logging for the HTTP transport only.

In stdio mode the server speaks JSON-RPC over stdout/stdin and must stay silent
on stdout; logging is therefore OFF unless explicitly enabled (which `main()`
does only for `--http`). The `@observe` wrapper is a passthrough until enabled,
so it has zero effect on stdio.

Each tool call emits one JSON line to stderr with: tool name, a COARSE lat/lon
bucket (1-degree, never full precision), latency, status, the `degraded` list,
and cache hit/miss counts for the call.
"""

import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .cache import start_metrics

logger = logging.getLogger("shearline.http")

_enabled = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "payload", None)
        if isinstance(payload, dict):
            return json.dumps(payload, default=str)
        return json.dumps({"level": record.levelname, "msg": record.getMessage()})


def configure(level: str = "INFO") -> None:
    """Install a JSON stderr handler on the shearline.http logger."""
    handler = logging.StreamHandler()  # stderr by default
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False


def enable() -> None:
    global _enabled
    _enabled = True


def is_enabled() -> bool:
    return _enabled


def _coarse(value: Any) -> int | None:
    return round(value) if isinstance(value, (int, float)) else None


def observe(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap a tool coroutine with HTTP-only structured logging.

    Uses functools.wraps so FastMCP still derives the original signature
    (the schema-lock test verifies the inputSchema is unchanged).
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _enabled:
            return await fn(*args, **kwargs)

        metrics = start_metrics()
        started = time.monotonic()
        lat = kwargs.get("lat", args[0] if len(args) >= 1 else None)
        lon = kwargs.get("lon", args[1] if len(args) >= 2 else None)
        status = "ok"
        degraded: list[str] = []
        try:
            result = await fn(*args, **kwargs)
            if isinstance(result, dict):
                degraded = result.get("degraded") or []
            return result
        except Exception:
            status = "error"
            raise
        finally:
            logger.info(
                "tool_call",
                extra={
                    "payload": {
                        "event": "tool_call",
                        "tool": fn.__name__,
                        "lat_bucket": _coarse(lat),
                        "lon_bucket": _coarse(lon),
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "status": status,
                        "degraded": degraded,
                        "cache_hits": metrics["hits"],
                        "cache_misses": metrics["misses"],
                    }
                },
            )

    return wrapper
