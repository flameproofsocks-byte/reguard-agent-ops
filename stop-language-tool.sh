#!/bin/bash
# Stops the language-learning web UI (Next.js dev server on port 8686).

PID_FILE="/tmp/language-learning-dev.pid"

if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill -- -"$PID" 2>/dev/null || kill "$PID"
    rm -f "$PID_FILE"
    echo "Stopped (PID $PID)."
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# Fallback: kill anything matching the dev command on port 8686
PIDS=$(pgrep -f "next dev -p 8686")
if [ -n "$PIDS" ]; then
  kill $PIDS
  echo "Stopped (PIDs: $PIDS)."
else
  echo "Not running."
fi
