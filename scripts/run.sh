#!/usr/bin/env bash
# Start the Dubious backend on http://localhost:8787 (the address the extension expects).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and set GEMINI_API_KEY." >&2
  exit 1
fi

UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
PORT="${DUBIOUS_PORT:-8787}"
# Loopback only. WSL forwards Windows' localhost to it, so Chrome on Windows can still reach it.
exec "$UV" run uvicorn server.app:app --host "${DUBIOUS_HOST:-127.0.0.1}" --port "$PORT" "$@"
