#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

"$DIR/stop-dashboard.sh"
sleep 3
"$DIR/start-dashboard.sh"
