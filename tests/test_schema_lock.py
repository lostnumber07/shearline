"""Schema-lock: freeze the tool I/O contract so a breaking change can't slip
through unnoticed.

The snapshot (tools_schema_snapshot.json) records, for every registered tool, its
MCP inputSchema (parameter names/types/defaults/required) and outputSchema, plus
the envelope SCHEMA_VERSION. The test rebuilds the manifest and compares; any
difference fails the suite with a unified diff.

This catches breaking changes to the *call* contract — a renamed/removed/retyped
parameter, a removed tool, a changed default, or a bumped schema version. Output
*data*-field presence is additionally guarded live by scripts/canary.py and by
each tool's own offline tests.

If a change is intentional: bump SCHEMA_VERSION per the ARCHITECTURE "Stability
contract" (major for a breaking rename/removal), then regenerate the snapshot:

    UPDATE_SCHEMA_SNAPSHOT=1 uv run pytest tests/test_schema_lock.py
"""

import difflib
import json
import os
from pathlib import Path

from shearline import server
from shearline.envelope import SCHEMA_VERSION

SNAPSHOT = Path(__file__).resolve().parent / "tools_schema_snapshot.json"


async def _manifest() -> dict:
    tools = await server.mcp.list_tools()
    return {
        "_schema_version": SCHEMA_VERSION,
        "tools": {
            t.name: {"inputSchema": t.inputSchema, "outputSchema": t.outputSchema}
            for t in sorted(tools, key=lambda t: t.name)
        },
    }


def _dump(obj: dict) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


async def test_tool_schema_matches_snapshot():
    manifest = await _manifest()

    if os.environ.get("UPDATE_SCHEMA_SNAPSHOT"):
        SNAPSHOT.write_text(_dump(manifest))
        return

    assert SNAPSHOT.exists(), (
        "schema snapshot missing — create it with "
        "UPDATE_SCHEMA_SNAPSHOT=1 uv run pytest tests/test_schema_lock.py"
    )
    expected = json.loads(SNAPSHOT.read_text())
    if manifest != expected:
        diff = "\n".join(
            difflib.unified_diff(
                _dump(expected).splitlines(),
                _dump(manifest).splitlines(),
                "snapshot",
                "current",
                lineterm="",
            )
        )
        raise AssertionError(
            "Tool I/O contract changed. If intentional, bump SCHEMA_VERSION per the "
            "ARCHITECTURE stability contract and regenerate:\n"
            "    UPDATE_SCHEMA_SNAPSHOT=1 uv run pytest tests/test_schema_lock.py\n\n"
            + diff
        )


async def test_snapshot_schema_version_is_current():
    """The snapshot's recorded version must match the code — forces a deliberate
    snapshot regen whenever the version is bumped."""
    expected = json.loads(SNAPSHOT.read_text())
    assert expected["_schema_version"] == SCHEMA_VERSION
