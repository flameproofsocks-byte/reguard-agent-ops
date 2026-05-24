#!/usr/bin/env bash

PIDFILE=/tmp/reguard-dashboard.pid

if [ ! -f "$PIDFILE" ]; then
    echo "No PID file found — dashboard may not be running."
    exit 0
fi

PID=$(cat "$PIDFILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm -f "$PIDFILE"
    echo "Dashboard stopped (PID $PID)"
else
    echo "Process $PID not found — cleaning up stale PID file."
    rm -f "$PIDFILE"
fi
