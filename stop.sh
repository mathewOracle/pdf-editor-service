#!/usr/bin/env bash
# Stop the server started by run.sh.
#
# Only ever kills the exact PID run.sh recorded -- never a broad
# pkill/lsof-based kill, which could hit some unrelated app that happens
# to be using a nearby port (see the README's troubleshooting section for
# exactly that happening with an "Ollama Usage Monitor" on port 8420).
set -euo pipefail

STATE_DIR="$HOME/.local/share/pdf-editor-service"
PID_FILE="$STATE_DIR/server.pid"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")"
  rm -f "$PID_FILE" "$STATE_DIR/server.port"
  echo "Stopped."
else
  echo "Not running (or was already stopped)."
fi
