#!/usr/bin/env bash
# Stop everything started by scripts/run_all.sh (services + infra).
set -u
cd "$(dirname "$0")/.."

PID_DIR="data/logs/pids"
say() { printf "\033[36m[stop-all]\033[0m %s\n" "$1"; }

if [ -d "$PID_DIR" ]; then
    for f in "$PID_DIR"/*.pid; do
        [ -e "$f" ] || continue
        pid=$(cat "$f")
        if kill -0 "$pid" 2>/dev/null; then
            say "stopping $(basename "$f" .pid) (pid $pid)"
            kill "$pid" 2>/dev/null
        fi
        rm -f "$f"
    done
fi

# belt-and-braces: catch stragglers by name
pkill -f "fieldpilot.run --backend" 2>/dev/null
pkill -f "fieldpilot.run --gui" 2>/dev/null
pkill -f "fieldpilot.run --source" 2>/dev/null
pkill -f "next dev" 2>/dev/null

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    say "stopping infra (docker compose down)"
    docker compose down
fi
say "done."
