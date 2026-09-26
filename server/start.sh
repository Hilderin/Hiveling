#!/usr/bin/env bash
# Hiveling server launcher (Linux/macOS).
#
# Starts server/dashboard.py: browser dashboard + JSON API + MCP endpoint.
#
# Usage:
#   ./server/start.sh                  # foreground on 127.0.0.1:8080
#   ./server/start.sh -d               # background (detached, pid file)
#   ./server/start.sh --port 9000 --host 0.0.0.0
#   HIVELING_HOST=0.0.0.0 HIVELING_PORT=9000 ./server/start.sh
#
# Env vars (used when the matching flag is absent):
#   HIVELING_HOST HIVELING_PORT HIVELING_DATA_DIR HIVELING_LOG_DIR
#   HIVELING_LOG_LEVEL HIVELING_HEARTBEAT HIVELING_KEEP_ALIVE HIVELING_TOKEN
#
# Known flags are handled here; any other argument is passed through to
# dashboard.py (e.g. --plans-dir, --workers-file, --poll-interval).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  PY="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "error: no python found (looked for .venv/bin/python and python3)" >&2
  exit 1
fi

HOST="${HIVELING_HOST:-127.0.0.1}"
PORT="${HIVELING_PORT:-8080}"
DATA_DIR="${HIVELING_DATA_DIR:-$PROJECT_ROOT/.data}"
LOG_DIR="${HIVELING_LOG_DIR:-}"
LOG_LEVEL="${HIVELING_LOG_LEVEL:-}"
HEARTBEAT="${HIVELING_HEARTBEAT:-}"
KEEP_ALIVE="${HIVELING_KEEP_ALIVE:-}"
DETACH=0
REST=()

usage() { sed -n 's/^# \?//p' "$0" | sed -n '6,22p'; }

while [ $# -gt 0 ]; do
  case "$1" in
    -d|--detach) DETACH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    --log-level) LOG_LEVEL="$2"; shift 2 ;;
    --heartbeat) HEARTBEAT="$2"; shift 2 ;;
    --keep-alive) KEEP_ALIVE="$2"; shift 2 ;;
    *) REST+=("$1"); shift ;;
  esac
done

ARGS=(--host "$HOST" --port "$PORT" --data-dir "$DATA_DIR")
[ -n "$LOG_DIR" ] && ARGS+=(--log-dir "$LOG_DIR")
[ -n "$LOG_LEVEL" ] && ARGS+=(--log-level "$LOG_LEVEL")
[ -n "$HEARTBEAT" ] && ARGS+=(--heartbeat "$HEARTBEAT")
[ -n "$KEEP_ALIVE" ] && ARGS+=(--keep-alive "$KEEP_ALIVE")
ARGS+=("${REST[@]}")

if ! mkdir -p "$DATA_DIR"; then
  echo "error: cannot create data dir $DATA_DIR" >&2
  exit 1
fi

if [ "$DETACH" -eq 1 ]; then
  LOGFILE="${LOG_DIR:-$DATA_DIR/logs}/server.console.log"
  mkdir -p "$(dirname "$LOGFILE")"
  nohup "$PY" "$SCRIPT_DIR/dashboard.py" "${ARGS[@]}" >>"$LOGFILE" 2>&1 &
  PID=$!
  echo "$PID" > "$DATA_DIR/server.pid"
  echo "Hiveling started in background (pid $PID) on ${HOST}:${PORT}"
  echo "  console log: $LOGFILE"
  echo "  pid file:    $DATA_DIR/server.pid"
else
  echo "Starting Hiveling on ${HOST}:${PORT} (data: $DATA_DIR)"
  exec "$PY" "$SCRIPT_DIR/dashboard.py" "${ARGS[@]}"
fi