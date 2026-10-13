#!/usr/bin/env bash
# Start the Offline PDF Editor in the background and print its URL.
#
# Safe to re-run: if it's already running (per the PID file this script
# itself writes), this just prints the existing URL instead of starting a
# second copy, which would otherwise fight over the same SQLite database.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Same directory app/db.py uses for the database -- kept outside the repo
# (and outside OneDrive) on purpose; see the README for why.
STATE_DIR="$HOME/.local/share/pdf-editor-service"
PID_FILE="$STATE_DIR/server.pid"
PORT_FILE="$STATE_DIR/server.port"
LOG_FILE="$STATE_DIR/server.log"
mkdir -p "$STATE_DIR"

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

if is_running; then
  echo "Already running (PID $(cat "$PID_FILE")) at http://127.0.0.1:$(cat "$PORT_FILE")/"
  exit 0
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/uvicorn" ]]; then
  echo "No .venv found. Run this first:" >&2
  echo "  uv venv" >&2
  exit 1
fi

# Pick the first free port from 8420 up. This app has no fixed port: some
# *other* local app grabbing 8420 before you start this one is exactly
# what makes the editor look "down" when it's actually just unreachable
# at the URL you tried. Scanning for a free port avoids that permanently.
PORT=8420
while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done

cd "$PROJECT_DIR"
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "$PORT" > "$PORT_FILE"

for _ in $(seq 1 30); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
    echo "Started (PID $(cat "$PID_FILE")) at http://127.0.0.1:$PORT/"
    if [[ "$(uname)" == "Darwin" ]]; then open "http://127.0.0.1:$PORT/"; fi
    exit 0
  fi
  sleep 0.3
done
echo "Started but isn't responding yet. Check the log:" >&2
echo "  tail -f $LOG_FILE" >&2
exit 1
