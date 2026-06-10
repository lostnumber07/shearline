"""Live smoke test: both transports, acceptance-gate points, timing.

Run: uv run python scripts/smoke.py
"""

import asyncio
import json
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


async def exercise(session: ClientSession, transport: str) -> None:
    tools = await session.list_tools()
    names = [t.name for t in tools.tools]
    assert len(names) == 7, f"expected 7 tools, got {names}"
    print(f"[{transport}] 7 tools listed OK")

    t0 = time.time()
    result = await session.call_tool(
        "get_active_warnings", {"lat": 43.914, "lon": -69.965}
    )
    dt = time.time() - t0
    payload = json.loads(result.content[0].text)
    assert payload["disclaimer"].startswith("Informational only")
    print(f"[{transport}] get_active_warnings (Brunswick): {dt:.1f}s — {payload['interpretation'][:90]}")

    t0 = time.time()
    result = await session.call_tool(
        "get_active_warnings", {"lat": 43.914, "lon": -69.965}
    )
    dt = time.time() - t0
    print(f"[{transport}] cached repeat: {dt*1000:.0f} ms (gate: <500 ms)")
    assert dt < 0.5, "cached call exceeded 500 ms"

    # out-of-bounds must error clearly, not crash
    result = await session.call_tool("get_spc_outlook", {"lat": 51.5, "lon": -0.12})
    assert result.isError
    assert "continental United States" in result.content[0].text
    print(f"[{transport}] out-of-bounds rejection OK")


async def main() -> None:
    t_start = time.time()
    params = StdioServerParameters(command="uv", args=["run", "shearline"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(f"[stdio] cold start to initialized: {time.time()-t_start:.1f}s (gate: <10s)")
            await exercise(session, "stdio")

    url = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else '8741'}/mcp"
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await exercise(session, "http")

    print("SMOKE PASSED")


asyncio.run(main())
