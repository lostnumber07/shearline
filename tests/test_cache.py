import asyncio

from shearline.cache import TTLCache


async def test_caches_within_ttl():
    cache = TTLCache()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"n": calls}

    first = await cache.get_or_fetch("k", 60, fetch)
    second = await cache.get_or_fetch("k", 60, fetch)
    assert first == second == {"n": 1}
    assert calls == 1


async def test_refetches_after_expiry():
    cache = TTLCache()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_fetch("k", 0.01, fetch) == 1
    await asyncio.sleep(0.03)
    assert await cache.get_or_fetch("k", 0.01, fetch) == 2


async def test_concurrent_callers_share_one_fetch():
    cache = TTLCache()
    calls = 0

    async def slow_fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return "value"

    results = await asyncio.gather(
        *(cache.get_or_fetch("k", 60, slow_fetch) for _ in range(10))
    )
    assert all(r == "value" for r in results)
    assert calls == 1


async def test_distinct_keys_fetch_separately():
    cache = TTLCache()

    async def fetch_a():
        return "a"

    async def fetch_b():
        return "b"

    assert await cache.get_or_fetch("a", 60, fetch_a) == "a"
    assert await cache.get_or_fetch("b", 60, fetch_b) == "b"


async def test_clear_forces_refetch():
    cache = TTLCache()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_fetch("k", 60, fetch)
    cache.clear()
    assert await cache.get_or_fetch("k", 60, fetch) == 2
