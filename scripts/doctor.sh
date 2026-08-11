#!/usr/bin/env bash
# FieldPilot AI environment check — reports, never fails hard.
set -u

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
# Neutral note: true, worth knowing, but not a problem to fix. Distinct from `warn` so that a
# green-but-for-one-note environment does not read as a broken one.
info() { printf "  \033[36m·\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }

echo "FieldPilot AI — environment check"

if command -v uv >/dev/null 2>&1; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
    bad "uv not found — install: https://docs.astral.sh/uv/"
fi

if [ -d .venv ]; then
    py=$(.venv/bin/python --version 2>/dev/null || echo "unknown")
    ok "venv present ($py)"
else
    warn ".venv missing — run: make setup"
fi

if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
        ok "docker daemon running ($(docker --version | awk '{print $3}' | tr -d ','))"
    else
        warn "docker installed but daemon not running (infra will be skipped)"
    fi
else
    warn "docker not found — backend falls back to SQLite + in-memory bus"
fi

if docker compose version >/dev/null 2>&1; then
    ok "docker compose available"
else
    warn "docker compose plugin missing"
fi

if [ -e /dev/video0 ]; then
    ok "camera /dev/video0 present"
else
    warn "no /dev/video0 — edge will use the synthetic source"
fi

# Spoken alerts are synthesised on the CLIENTS now — the worker's phone (flutter_tts) and the
# dashboard (Web Speech API) — because the server's speakers are not where anyone is listening.
# espeak-ng only affects the legacy server-side path in `alerts/tts.py`, so its absence is a note,
# not a warning: reporting it as a problem implied spoken alerts were broken when they were not.
if command -v espeak-ng >/dev/null 2>&1 || command -v espeak >/dev/null 2>&1; then
    ok "espeak-ng present (server-side TTS available)"
else
    info "espeak-ng absent — only affects server-side TTS; spoken alerts run on the phone and dashboard"
fi

if nvidia-smi >/dev/null 2>&1; then
    gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    ok "GPU: $gpu"
else
    warn "no NVIDIA GPU detected — inference runs on CPU"
fi

for m in models/yolo11m-pose.pt models/ppe_css.pt; do
    if [ -f "$m" ]; then ok "model $m"; else warn "model $m missing (auto-downloads on first run)"; fi
done
