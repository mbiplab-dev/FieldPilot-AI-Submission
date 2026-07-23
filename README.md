# FieldPilot AI

Real-time, multimodal edge-AI for construction-site safety, spatial compliance, and automated
reporting. Display-less: all feedback is delivered through audio (earcons + speech) and haptics.

> ⚠️ **Advisory system.** FieldPilot assists workers; it is not an authoritative safety control and
> must not be relied on as the sole means of hazard detection. See [`docs/`](./docs).

See [`plan.md`](./plan.md) for the architecture and [`tracking.md`](./tracking.md) for build status.

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

## Configuration

Everything tunable lives in [`config.yaml`](./config.yaml). Any value can be overridden with an env
var of the form `FIELDPILOT_<SECTION>__<KEY>` (double underscore denotes nesting), e.g.
`FIELDPILOT_APP__PERSPECTIVE=BOTH`.

## Later milestones

- **M2** — LiveKit streaming ingest, LiteRT INT8 edge export, OpenCV measurement/calibration.
- **M3** — `docker compose up -d` brings up PostgreSQL, Redis, Qdrant, Ollama for the feedback
  learning loop, blueprint RAG, RFI drafting, cross-worker broadcast, and the HTMX dashboard.
