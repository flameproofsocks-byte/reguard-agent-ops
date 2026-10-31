#!/bin/bash
# Starts the language-learning web UI (Next.js dev server on port 8686).

PROJECT_DIR="/home/brady/coding/reguardtrading/projects/language-learning"
LOG_FILE="/tmp/language-learning-dev.log"
PID_FILE="/tmp/language-learning-dev.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PID_FILE"))."
  exit 0
fi

cd "$PROJECT_DIR" || exit 1
setsid nohup npm run dev > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Started language-learning dev server (PID $!). Logs: $LOG_FILE"
echo "URL: http://localhost:8686"
