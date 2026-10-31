#!/usr/bin/env bash
set -e

PIDFILE=/tmp/reguard-dashboard.pid
LOGFILE=/tmp/reguard-dashboard.log
DIR="$(cd "$(dirname "$0")" && pwd)"
HEADLESS=0

for arg in "$@"; do
    [ "$arg" = "--headless" ] && HEADLESS=1
done

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Dashboard already running (PID $(cat "$PIDFILE"))"
    [ "$HEADLESS" = "0" ] && echo "  → http://localhost:6969/agents"
    exit 0
fi

cd "$DIR"

if [ "$HEADLESS" = "1" ]; then
    nohup .venv/bin/python dashboard/server.py --headless > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Dashboard started in headless mode (PID $(cat "$PIDFILE")) — Telegram only"
        echo "Logs: $LOGFILE"
    else
        echo "Dashboard failed to start. Check logs: $LOGFILE"
        exit 1
    fi
else
    nohup .venv/bin/uvicorn dashboard.server:app --host 0.0.0.0 --port 6969 > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"

    for i in $(seq 1 10); do
        sleep 0.5
        if curl -s http://localhost:6969/api/agents > /dev/null 2>&1; then
            echo "Dashboard started (PID $(cat "$PIDFILE")) — http://localhost:6969/agents"
            echo "Logs: $LOGFILE"
            exit 0
        fi
    done

    echo "Dashboard may still be starting. Check logs: $LOGFILE"
fi
