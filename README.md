# FieldPilot AI

Real-time, multimodal edge-AI for construction-site safety, spatial compliance, and automated
reporting. Display-less: all feedback is delivered through audio (earcons + speech) and haptics.

> ⚠️ **Advisory system.** FieldPilot assists workers; it is not an authoritative safety control and
> must not be relied on as the sole means of hazard detection. See [`docs/`](./docs).

See [`plan.md`](./plan.md) for the architecture and [`tracking.md`](./tracking.md) for build status.

## Quick start with Make (recommended)

```bash
make setup        # install everything (uv)
make doctor       # check your environment
make run-all      # start EVERYTHING: infra + backend :8100 + edge pipeline
                  # Ctrl-C (or `make stop-all`) tears it all down cleanly
```

| Target | What it does |
|---|---|
| `make run-all` | One-command platform: infra → backend → edge (feed+bus) → Next.js dashboard |
| `make stop-all` | Stop services + infra |
| `make backend` | Event-driven backend only (`http://localhost:8100`, docs at `/docs`) |
| `make edge` / `make edge-synthetic` | Edge safety loop on webcam / synthetic frames |
| `make gui` | Live MJPEG dashboard on `http://localhost:8000` |
| `make frontend` | Next.js site-manager dashboard on `http://localhost:3000` |
| `make infra-up` / `infra-down` | PostgreSQL + Redis + Qdrant + Ollama |
| `make test` / `make lint` | 74-test suite / ruff + eslint |
| `make llm-pull` | Pull the llama3.2:3b model for alert verification |
| `make validate` / `make bench` | 10-min stress run / latency harness |
| `make help` | All targets |

## Quick start (Milestone 1 — offline edge safety loop)

Runs with **no cloud, no API keys, no internet**. Local espeak-ng provides the voice fallback.

```bash
# 1. Create the pinned Python 3.12 environment (system Python 3.14 is too new for the ML stack)
uv sync

# 2. Run against your webcam (/dev/video0)
uv run python -m fieldpilot.run --source webcam

# 3. Or against a video file
uv run python -m fieldpilot.run --source file --file data/videos/sample.mp4

# 4. Repeatable 10-minute validation run (headless, exits 0 on success)
uv run python -m fieldpilot.run --validate 10min

# 5. Latency harness (prints median detection→alert latency; target < 500 ms)
uv run python -m fieldpilot.run --bench

# 6. Tests
uv run pytest tests -k "fall or attention"
```

The first run auto-downloads `yolov8n-pose.pt` into `models/`.

## Event-driven backend (Phase 1–3, 8)

AI models never call notification/dashboard APIs directly — they publish canonical
events. The platform chain is:

```
Model → Event → Trigger Engine (dedup/merge/resolve) → Rules Engine → Notification → Dashboard
```

```bash
# 1. (optional) start PostgreSQL + Redis + Qdrant + Ollama
docker compose up -d          # without this, the backend runs on SQLite + in-memory bus

# 2. start the backend service (bus + triggers + rules + REST) on :8100
uv run python -m fieldpilot.run --backend

# 3. ingest an event (any model / edge device)
curl -X POST http://localhost:8100/events -H 'Content-Type: application/json' -d '{
  "worker_id": "w-1", "camera_id": "cam-1", "zone": "zone-a",
  "event_type": "ppe", "severity": "medium", "confidence": 0.92,
  "payload": {"ppe_item": "helmet", "dedup_key": "helmet", "message": "no helmet"}
}'

# 4. query the deduplicated alert board, rules, worker timeline
curl http://localhost:8100/alerts
curl http://localhost:8100/rules
curl http://localhost:8100/workers/w-1/timeline

# 5. run the edge pipeline in event-driven mode (detections publish onto the bus)
uv run python -m fieldpilot.run --source webcam --bus
```

The trigger engine collapses duplicate detections of the same issue within 45 s, merges
repeats into one alert with a hit counter, tracks alerts (NEW → ACTIVE), auto-resolves
when the issue disappears, and lets operators SUPPRESS noise. Rules are stored in the
database and edited via REST — e.g. *no helmet AND in danger zone → critical alert*;
*crack severity > 0.85 → immediate inspection*; *rebar deviation > 20 mm → generate RFI*.

## Configuration

Everything tunable lives in [`config.yaml`](./config.yaml). Any value can be overridden with an env
var of the form `FIELDPILOT_<SECTION>__<KEY>` (double underscore denotes nesting), e.g.
`FIELDPILOT_APP__PERSPECTIVE=BOTH`.

## Later milestones

- **M2** — LiveKit streaming ingest, LiteRT INT8 edge export, OpenCV measurement/calibration.
- **M3** — `docker compose up -d` brings up PostgreSQL, Redis, Qdrant, Ollama for the feedback
  learning loop, blueprint RAG, RFI drafting, cross-worker broadcast, and the HTMX dashboard.
