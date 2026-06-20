#!/bin/bash
# SHEARLINE launcher — starts the streamable-HTTP MCP server in this window.
# Double-click to run; press Ctrl-C (or close the window) to stop.
#
# Prefers the published package via `uvx shearline`; falls back to a local
# checkout (`uv run`) when this script lives inside one.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
PORT="${SHEARLINE_PORT:-8741}"
URL="http://127.0.0.1:${PORT}/mcp"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

printf '\n  ⛈  SHEARLINE — severe-weather MCP server\n'
printf '  ─────────────────────────────────────────\n'
printf '  Serving (streamable HTTP) at:  %s\n' "$URL"
printf '  Point an MCP client at that URL as a streamable-HTTP server.\n'
printf '  Press Ctrl-C or close this window to stop.\n\n'

if command -v uvx >/dev/null 2>&1; then
  uvx shearline --http --port "$PORT"
elif command -v uv >/dev/null 2>&1 && [ -f "$SELF/../pyproject.toml" ]; then
  cd "$SELF/.." && uv run shearline --http --port "$PORT"
else
  printf '  ERROR: could not find uvx or uv on PATH.\n'
  printf '  Install uv (https://docs.astral.sh/uv/), then re-launch.\n'
fi

status=$?
printf '\n  SHEARLINE server stopped (exit %s).\n' "$status"
read -n 1 -s -r -p "  Press any key to close this window..."
printf '\n'
