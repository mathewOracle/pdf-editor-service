#!/usr/bin/env bash
# Is the editor running, and where? Reads the same PID/port files run.sh
# writes, so this always reports the truth instead of a guessed port.
set -euo pipefail

STATE_DIR="$HOME/.local/share/pdf-editor-service"
PID_FILE="$STATE_DIR/server.pid"
PORT_FILE="$STATE_DIR/server.port"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  PORT="$(cat "$PORT_FILE" 2>/dev/null || echo "?")"
  echo "Running (PID $(cat "$PID_FILE")) at http://127.0.0.1:$PORT/"
else
  echo "Not running. Start it with ./run.sh"
fi
