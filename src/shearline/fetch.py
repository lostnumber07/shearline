"""Shared HTTP plumbing: one async client, polite User-Agent, cached GETs."""

import asyncio
import logging
import os
from typing import Any

import httpx

from .cache import CACHE

# httpx logs every request at INFO; that's noise on an MCP server's stderr.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Cap concurrent outbound upstream fetches as politeness toward NWS/SPC/NOMADS/IEM
# (the cache already collapses duplicate fetches; this bounds distinct ones across
# all clients). Created lazily per event loop so it never binds across test loops.
UPSTREAM_CONCURRENCY = int(os.environ.get("SHEARLINE_UPSTREAM_CONCURRENCY", "8"))
_sems: dict[Any, asyncio.Semaphore] = {}


def _upstream_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(UPSTREAM_CONCURRENCY)
        _sems[loop] = sem
    return sem

USER_AGENT = "shearline/1.0 (+https://github.com/lostnumber07/shearline)"

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    return _client


async def get_json(
    url: str,
    *,
    ttl: float,
    cache_key: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    async def fetch() -> Any:
        last: Exception | None = None
        async with _upstream_sem():
            for _attempt in range(2):  # one retry — NOAA endpoints hiccup transiently
                try:
                    resp = await client().get(url, headers=headers or {})
                    resp.raise_for_status()
                    return resp.json()
                except httpx.HTTPError as exc:
                    last = exc
        raise last  # type: ignore[misc]

    return await CACHE.get_or_fetch(cache_key or f"json:{url}", ttl, fetch)


async def get_bytes(
    url: str,
    *,
    ttl: float,
    cache_key: str | None = None,
) -> bytes:
    async def fetch() -> bytes:
        async with _upstream_sem():
            resp = await client().get(url)
            resp.raise_for_status()
            return resp.content

    return await CACHE.get_or_fetch(cache_key or f"bytes:{url}", ttl, fetch)
