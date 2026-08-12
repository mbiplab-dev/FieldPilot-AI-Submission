#!/usr/bin/env bash
# FieldPilot AI — run the WHOLE platform with one command.
#
#   infra (PostgreSQL + Redis + Qdrant + Ollama)
#     → backend service :8100 (bus + triggers + rules + REST)
#       → edge pipeline (webcam or synthetic) publishing events onto the bus
#
# Ctrl-C tears everything down cleanly. Logs live in data/logs/.
set -u
cd "$(dirname "$0")/.."

BACKEND_PORT="${BACKEND_PORT:-8100}"
GUI_PORT="${GUI_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
LOG_DIR="data/logs"
PID_DIR="$LOG_DIR/pids"
INFRA_STARTED=0
BACKEND_PID=""
EDGE_PID=""
FRONTEND_PID=""

say()  { printf "\033[36m[run-all]\033[0m %s\n" "$1"; }
warn() { printf "\033[33m[run-all]\033[0m %s\n" "$1"; }
die()  { printf "\033[31m[run-all]\033[0m %s\n" "$1" >&2; exit 1; }
port_busy() { ss -H -ltn "sport = :$1" 2>/dev/null | rg -q .; }

mkdir -p "$PID_DIR"

cleanup() {
    say "shutting down…"
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null
    [ -n "$EDGE_PID" ]     && kill "$EDGE_PID"     2>/dev/null
    [ -n "$BACKEND_PID" ]  && kill "$BACKEND_PID"  2>/dev/null
    rm -f "$PID_DIR"/*.pid 2>/dev/null
    if [ "$INFRA_STARTED" = "1" ]; then
        say "stopping infra (docker compose down)"
        docker compose down >/dev/null 2>&1
    fi
    say "done."
    exit 0
}
trap cleanup INT TERM

# ------------------------------------------------------------------ 1. infra
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    SERVICES=()
    USE_POSTGRES=0
    USE_REDIS=0

    if port_busy 5432; then
        warn "port 5432 is already in use — using SQLite instead of starting FieldPilot Postgres"
    else
        SERVICES+=(postgres)
        USE_POSTGRES=1
    fi
    if port_busy 6379; then
        warn "port 6379 is already in use — using the in-memory event bus"
    else
        SERVICES+=(redis)
        USE_REDIS=1
    fi
    if port_busy 6333; then
        warn "port 6333 is already in use — not starting FieldPilot Qdrant"
    else
        SERVICES+=(qdrant)
    fi

    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        # A developer-installed Ollama already owns :11434 and, unlike a fresh container, may
        # already hold the multi-gigabyte Gemma model. Starting the compose Ollama here would fail
        # on the port collision and make a prepared hackathon laptop look broken.
        say "host Ollama detected — starting available FieldPilot infrastructure"
    else
        if port_busy 11434; then
            warn "port 11434 is occupied by a non-Ollama service — local assistant may be unavailable"
        else
            SERVICES+=(ollama)
        fi
    fi

    if [ "${#SERVICES[@]}" -gt 0 ]; then
        docker compose up -d "${SERVICES[@]}" >/dev/null || die "docker compose up failed"
        INFRA_STARTED=1
    fi

    if [ "$USE_POSTGRES" = "1" ]; then
        say "waiting for postgres…"
        for _ in $(seq 1 30); do
            docker compose exec -T postgres pg_isready -U fieldpilot >/dev/null 2>&1 && break
            sleep 1
        done
        export FIELDPILOT_EVENTS__BACKEND=postgres
        export FIELDPILOT_EVENTS__DATABASE_URL="postgresql+psycopg://fieldpilot:fieldpilot@localhost:5432/fieldpilot"
        export FIELDPILOT_EVENTS__EVENTS_DB_URL="postgresql+psycopg://fieldpilot:fieldpilot@localhost:5432/fieldpilot"
    fi
    if [ "$USE_REDIS" = "1" ]; then
        say "waiting for redis…"
        for _ in $(seq 1 30); do
            [ "$(docker compose exec -T redis redis-cli ping 2>/dev/null)" = "PONG" ] && break
            sleep 1
        done
        export FIELDPILOT_EVENTS__BUS_BACKEND=redis
        export FIELDPILOT_EVENTS__REDIS_URL="redis://localhost:6379/0"
    fi

    say "infrastructure ready — unavailable services use local fallbacks"
else
    warn "docker unavailable — backend falls back to SQLite + in-memory bus"
fi

# ------------------------------------------------------------------ 2. backend
say "starting backend on :$BACKEND_PORT (log: $LOG_DIR/backend.log)"
uv run python -m fieldpilot.run --backend --port "$BACKEND_PORT" \
    > "$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$PID_DIR/backend.pid"

say "waiting for backend /health…"
healthy=0
for _ in $(seq 1 45); do
    if curl -sf "http://localhost:$BACKEND_PORT/health" >/dev/null 2>&1; then
        healthy=1; break
    fi
    kill -0 "$BACKEND_PID" 2>/dev/null || { tail -20 "$LOG_DIR/backend.log"; die "backend crashed"; }
    sleep 1
done
[ "$healthy" = "1" ] || die "backend did not become healthy — see $LOG_DIR/backend.log"
say "backend up:  http://localhost:$BACKEND_PORT  (docs: /docs)"

# ------------------------------------------------------------------ 3. edge (live feed + bus)
if [ -e /dev/video0 ]; then
    SRC="webcam"
else
    SRC="synthetic"
    warn "no /dev/video0 — using synthetic frames"
fi
say "starting edge pipeline (source=$SRC, gui+feed on :$GUI_PORT, mode=event bus)…"
uv run python -m fieldpilot.run --gui --source "$SRC" --bus --port "$GUI_PORT" \
    > "$LOG_DIR/edge.log" 2>&1 &
EDGE_PID=$!
echo "$EDGE_PID" > "$PID_DIR/edge.pid"
sleep 4
kill -0 "$EDGE_PID" 2>/dev/null || { tail -20 "$LOG_DIR/edge.log"; die "edge pipeline crashed"; }

# ------------------------------------------------------------------ 4. frontend dashboard
if [ -d frontend ] && command -v npm >/dev/null 2>&1; then
    say "starting Next.js dashboard on :$FRONTEND_PORT (log: $LOG_DIR/frontend.log)"
    (cd frontend && npm run dev -- --port "$FRONTEND_PORT" > "../$LOG_DIR/frontend.log" 2>&1) &
    FRONTEND_PID=$!
    echo "$FRONTEND_PID" > "$PID_DIR/frontend.pid"
    say "waiting for dashboard…"
    for _ in $(seq 1 60); do
        curl -sf "http://localhost:$FRONTEND_PORT" >/dev/null 2>&1 && break
        kill -0 "$FRONTEND_PID" 2>/dev/null || break
        sleep 2
    done
else
    warn "frontend/ or npm missing — dashboard not started"
fi

say "────────────────────────────────────────────────────────────"
say "FieldPilot AI is LIVE"
say "  ▸ dashboard   http://localhost:$FRONTEND_PORT        (site manager UI)"
say "  ▸ backend     http://localhost:$BACKEND_PORT        (REST API · docs at /docs)"
say "  ▸ live feed   http://localhost:$GUI_PORT/stream     (annotated MJPEG)"
say "  ▸ edge GUI    http://localhost:$GUI_PORT             (engineering view)"
say "  ▸ alerts      curl http://localhost:$BACKEND_PORT/alerts"
say "  ▸ inspection  curl -X POST http://localhost:$BACKEND_PORT/control/inspection \\"
say "                  -H 'Content-Type: application/json' -d '{\"enabled\":true}'"
say "  ▸ logs        tail -f $LOG_DIR/backend.log $LOG_DIR/edge.log $LOG_DIR/frontend.log"
say "  ▸ stop        Ctrl-C   (or: make stop-all)"
say "  ▸ voice demo  worker app → Pilot → tap beacon → 'Hey FieldPilot, measure this'"
say "────────────────────────────────────────────────────────────"

# foreground wait — Ctrl-C lands here and triggers cleanup
wait "$EDGE_PID" 2>/dev/null
cleanup
