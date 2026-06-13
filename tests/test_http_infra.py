"""Tests for Task 7: HTTP-transport observability + rate limiting.

All offline and synchronous-ish: the rate limiter is driven through a tiny ASGI
harness, the observe wrapper through a capturing log handler, and cache metrics
directly.
"""

import json
import logging

import pytest

from shearline import observability
from shearline.cache import CACHE, TTLCache, start_metrics
from shearline.observability import observe
from shearline.ratelimit import RateLimitMiddleware


@pytest.fixture(autouse=True)
def _reset():
    CACHE.clear()
    observability._enabled = False
    yield
    observability._enabled = False
    CACHE.clear()


# --- cache hit/miss metrics --------------------------------------------------


async def test_cache_metrics_counts_hits_and_misses():
    cache = TTLCache()
    m = start_metrics()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_fetch("k", 60, fetch)  # miss
    await cache.get_or_fetch("k", 60, fetch)  # hit
    await cache.get_or_fetch("k", 60, fetch)  # hit
    assert m == {"hits": 2, "misses": 1}


async def test_cache_metrics_inert_without_scope():
    # No start_metrics() called -> get_or_fetch must not raise.
    cache = TTLCache()

    async def fetch():
        return 1

    assert await cache.get_or_fetch("k", 60, fetch) == 1


# --- observe wrapper ---------------------------------------------------------


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _attach_capture() -> Capture:
    cap = Capture()
    observability.logger.handlers = [cap]
    observability.logger.setLevel(logging.INFO)
    observability.logger.propagate = False
    return cap


async def test_observe_is_passthrough_and_silent_when_disabled():
    cap = _attach_capture()

    @observe
    async def tool(lat, lon, radius_km=40):
        return {"degraded": []}

    out = await tool(35.47, -97.52)
    assert out == {"degraded": []}
    assert cap.records == []  # nothing logged in stdio mode


async def test_observe_logs_structured_line_when_enabled():
    cap = _attach_capture()
    observability.enable()

    @observe
    async def get_demo(lat, lon):
        return {"degraded": ["mrms-x"]}

    await get_demo(lat=35.47, lon=-97.52)
    assert len(cap.records) == 1
    payload = cap.records[0].payload
    assert payload["tool"] == "get_demo"
    assert payload["lat_bucket"] == 35 and payload["lon_bucket"] == -98  # coarse, 1-deg
    assert payload["degraded"] == ["mrms-x"]
    assert payload["status"] == "ok"
    assert "latency_ms" in payload and "cache_hits" in payload
    # the JSON formatter emits a single parseable line
    assert json.loads(observability.JsonFormatter().format(cap.records[0]))["tool"] == "get_demo"


async def test_observe_logs_error_status_and_reraises():
    cap = _attach_capture()
    observability.enable()

    @observe
    async def get_boom(lat, lon):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await get_boom(lat=40.0, lon=-90.0)
    assert cap.records[0].payload["status"] == "error"


# --- rate limit: token bucket logic ------------------------------------------


def test_bucket_allows_up_to_burst_then_denies():
    mw = RateLimitMiddleware(app=None, rpm=60, burst=3)
    now = 1000.0
    assert mw._allow("ip", now)[0] is True
    assert mw._allow("ip", now)[0] is True
    assert mw._allow("ip", now)[0] is True
    allowed, retry = mw._allow("ip", now)
    assert allowed is False and retry > 0


def test_bucket_refills_over_time():
    mw = RateLimitMiddleware(app=None, rpm=60, burst=1)  # 1 token/sec
    now = 1000.0
    assert mw._allow("ip", now)[0] is True
    assert mw._allow("ip", now)[0] is False
    # one second later a token is back
    assert mw._allow("ip", now + 1.01)[0] is True


def test_buckets_are_per_client():
    mw = RateLimitMiddleware(app=None, rpm=60, burst=1)
    now = 1000.0
    assert mw._allow("a", now)[0] is True
    assert mw._allow("b", now)[0] is True  # different client unaffected


# --- rate limit: ASGI behavior -----------------------------------------------


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _drive(mw, client="1.2.3.4", headers=None):
    scope = {"type": "http", "headers": headers or [], "client": (client, 5555)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


async def test_asgi_allows_then_429s():
    mw = RateLimitMiddleware(app=_ok_app, rpm=60, burst=1)
    first = await _drive(mw)
    assert first[0]["status"] == 200

    second = await _drive(mw)
    assert second[0]["status"] == 429
    headers = dict(second[0]["headers"])
    assert b"retry-after" in headers
    body = json.loads(second[1]["body"])
    assert body["error"] == "rate_limited"
    assert "disclaimer" in body


async def test_asgi_uses_x_forwarded_for():
    mw = RateLimitMiddleware(app=_ok_app, rpm=60, burst=1)
    xff = [(b"x-forwarded-for", b"9.9.9.9, 10.0.0.1")]
    assert (await _drive(mw, client="1.1.1.1", headers=xff))[0]["status"] == 200
    # same XFF client is now limited even from a different direct IP
    assert (await _drive(mw, client="2.2.2.2", headers=xff))[0]["status"] == 429


async def test_asgi_passes_through_non_http_scope():
    seen = {}

    async def app(scope, receive, send):
        seen["type"] = scope["type"]

    mw = RateLimitMiddleware(app=app, rpm=60, burst=1)
    await mw({"type": "lifespan"}, None, None)
    assert seen["type"] == "lifespan"


def test_from_env_disabled_returns_app_unchanged(monkeypatch):
    monkeypatch.setenv("SHEARLINE_RATE_RPM", "0")
    sentinel = object()
    assert RateLimitMiddleware.from_env(sentinel) is sentinel
