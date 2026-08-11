# FieldPilot AI — Implementation Plan

Real-time, multimodal edge-AI for construction-site safety, spatial compliance, and automated
reporting. This is the living implementation plan; day-to-day status lives in [`tracking.md`](./tracking.md).

## What this is

A display-less wearable safety assistant. A camera (webcam now, Meta Ray-Ban glasses later) feeds a
real-time vision pipeline that detects hazards (falls, missing PPE), verifies whether the worker
*noticed* the hazard, checks spatial compliance against blueprints, and delivers alerts through
audio (earcons + speech) and haptics. Supervisor feedback closes the loop and fine-tunes the models.

## Confirmed decisions

| Area | Decision |
|------|----------|
| Camera (now) | Laptop / mobile webcam (`/dev/video0`). Meta glasses deferred to when hardware arrives. |
| Perspective | **Both** ego (wearer state via IMU/orientation) and exo (observed workers via camera). |
| Build order | **Depth-first** — one working end-to-end safety loop first, then widen. |
| LLM + embeddings | **Fully local Ollama** (offline-capable). |
| TTS | **Cloud TTS primary + local (espeak-ng/pyttsx3) fallback** so alerts still speak offline. |
| Dashboard | **FastAPI + HTMX**, served by the same backend. |
| Runtime | **Python 3.12 via `uv`** (system Python 3.14 is too new for the ML stack). GPU: RTX 3050 6GB. |

## Corrections applied to the original PRD

1. **Faster-Whisper is speech-to-text, not text-to-speech.** Real TTS engine drives voice alerts;
   Whisper is kept only as *optional voice-command input* (STT), correctly labeled.
2. **INT8/FP32 quantization is stated honestly.** Weight-only INT8 saves memory but runs float
   matmuls (no INT8 compute speedup); the export path benchmarks this rather than claiming both.
3. **PPE detection is a separate model.** YOLOv8-Pose only yields person keypoints; hardhat/vest
   detection uses a second, pluggable object detector.
4. **Ego vs exo resolved.** Exo = observed workers from the camera. Ego = wearer state from device
   IMU/orientation (never solvePnP on the wearer's own unseeable face).
5. **No "zero-hallucination" / "guaranteed mAP gain" guarantees.** The learning loop is
   *measure-and-gate*: it logs the mAP50 delta on a locked val set and promotes weights only if the
   delta is ≥ 0; regressions are recorded, not assumed away.
6. **Safety posture is advisory, not authoritative** — fail-safe on model/stream failure, explicit
   false-negative handling, and a privacy/consent note in `docs/`.

## Architecture

**The one rule: AI models never call notification/dashboard APIs directly.** Every model
publishes a canonical event; everything observable flows from the engine chain:

```
Model → Event → Trigger Engine → Rules Engine → Notification → Dashboard
```

```
[Webcam / video file / (later) LiveKit stream]
        │  async frame producer → bounded queue
        ▼
[core] VisionEngine ── YOLOv8-Pose (stream=True, persist=True) + BoT-SORT
        │                └─ PPE detector (separate model, pluggable)
        ├─ [safety] FallDetector          (rolling shoulder/hip/ankle velocity)
        ├─ [safety] AttentionStateMachine (Passive→Unnoticed→Escalated, 600ms dwell)
        │              gaze: solvePnP head-pose (exo) | IMU/orientation (ego)
        ▼
[events.bridge] HazardEvent → canonical Event ──publish──▶ [events.bus]  (memory | Redis)
                                                                │
                                ┌───────────────────────────────┼───────────────────────────┐
                                ▼                               ▼                           ▼
                    [events.store] raw event log      [triggers.engine]          (other subscribers:
                     PostgreSQL / SQLite               dedup 45 s · merge ·       learning loop,
                                                     NEW/ACTIVE/RESOLVED/         analytics…)
                                                     SUPPRESSED · auto-resolve
                                                                │ alerts.new / .resolved
                                                                ▼
                                                     [rules.engine] DB-configurable rules
                                                     IF conditions THEN create_alert /
                                                     generate_rfi / request_inspection / notify
                                                                │
                                            ┌───────────────────┼───────────────────┐
                                            ▼                   ▼                   ▼
                                  [notifications.service]  [rfis] store      [inspections] store
                                   dedup · retry · channels:
                                   dashboard/sms/email/whatsapp/push
                                                                │
                                                                ▼
                                        [backend.app] REST: /events /alerts /rules
                                        /workers/{id}/timeline /notifications /rfis /inspections
Infra (docker-compose): PostgreSQL · Redis · Qdrant · Ollama
```

The offline M1 edge loop (earcons + TTS + haptics via `alerts.Dispatcher`) still works
standalone (`--source webcam`); with `--bus` the same detections publish onto the event
bus instead, and local dispatch becomes just another downstream consumer.

## Milestones

- **M0 — Scaffold & deliverables:** repo skeleton, `uv`/py3.12, `config.yaml`, structured logging +
  SQLite event store, this plan + tracking.
- **M1 — Tier-1 edge safety loop (first light):** video source → YOLOv8-Pose + BoT-SORT → fall +
  attention + PPE → earcon/TTS/haptic alerts → SQLite. Runs with **no cloud, no keys, no internet**.
  Latency harness targets <500 ms detection→alert.
- **M2 — Streaming, calibration, real UI:** LiveKit ingest, LiteRT INT8 export + honest benchmark,
  OpenCV calibration/measurement, full solvePnP head-pose + IMU ego path, finalized earcons + cloud TTS.
- **M3 — Backend, RAG, learning, scale:** docker-compose infra, FastAPI feedback API, fine-tune +
  mAP50 gate, Qdrant RAG with zone filtering, RFI drafting, Redis zone broadcast, offline
  store-and-forward, HTMX dashboard.

## Repository layout

```
fieldpilot/{core,safety,compliance,alerts,reasoning,learning,display,logging_}/  perspective.py
fieldpilot/{events,triggers,rules,notifications,backend}/
config.yaml  pyproject.toml  docker-compose.yaml  .env.example
data/{videos,blueprints,val_set}/   models/   docs/   tests/
```

## Verification (per milestone)

- **M1:** `uv run python -m fieldpilot.run --source webcam`; `--source data/videos/sample.mp4`;
  `--validate 10min`; `pytest tests -k "fall or attention"`; latency harness < 500 ms.
- **M2:** calibration unit tests, solvePnP sanity, LiteRT export loads, benchmark doc generated.
- **M3:** `docker compose up -d`; RAG zone-isolation (negative test), fine-tune logs mAP50 delta &
  gates, WebSocket kill → reconnect → zero dropped/duplicated events, dashboard live feed.
