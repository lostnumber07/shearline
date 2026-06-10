"""Shared HTTP plumbing: one async client, polite User-Agent, cached GETs."""

import logging
from typing import Any

import httpx

from .cache import CACHE

# httpx logs every request at INFO; that's noise on an MCP server's stderr.
logging.getLogger("httpx").setLevel(logging.WARNING)

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
        resp = await client().get(url, headers=headers or {})
        resp.raise_for_status()
        return resp.json()

    return await CACHE.get_or_fetch(cache_key or f"json:{url}", ttl, fetch)


async def get_bytes(
    url: str,
    *,
    ttl: float,
    cache_key: str | None = None,
) -> bytes:
    async def fetch() -> bytes:
        resp = await client().get(url)
        resp.raise_for_status()
        return resp.content

    return await CACHE.get_or_fetch(cache_key or f"bytes:{url}", ttl, fetch)
