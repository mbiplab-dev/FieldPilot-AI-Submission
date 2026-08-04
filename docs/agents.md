# The multi-agent architecture, mapped to code

This is the reference architecture the project is built against:

```
                        ┌──────────────────┐
                        │   Meta Glasses   │
                        └────────┬─────────┘
        Video/Audio via Bluetooth│   ▲ Audio Feedback
                                 ▼   │
                        ┌──────────────────┐
                        │ Pocket Mobile    │
                        │ Phone Edge Node  │◄──────────────┐
                        └────────┬─────────┘               │
                  WebRTC / RTMP  │                         │
                                 ▼                         │
                        ┌──────────────────┐               │
                        │ Cloud Ingestion  │               │
                        └───┬──────────┬───┘               │
  ┌─────────────────────────┼──────────┼───────────────────┼──────────────┐
  │ Multi-Agent System      ▼          ▼                   │              │
  │            ┌──────────────────┐  ┌─────────────┐       │              │
  │            │ A1 Vision        │  │ Voice Audio │       │ TTS Audio    │
  │            │    Ingestion     │  └──────┬──────┘       │ Alert        │
  │            └───┬──────────┬───┘         ▼              │              │
  │                │          ▼      ┌─────────────┐       │              │
  │                │  ┌────────────┐ │ A5 Voice/   │       │              │
  │                │  │ A2 Measure │ │    NLP      │       │              │
  │                │  └──────┬─────┘ └──────┬──────┘       │              │
  │                ▼         ▼              ▼              │              │
  │      ┌──────────────┐ ┌────────────┐ ┌──────────────┐  │              │
  │      │ A4 Hazard/   │ │ A3         │ │ A7 Knowledge │  │              │
  │      │    Safety    │ │ Compliance │ │   Retrieval  │  │              │
  │      └───┬──────┬───┘ └──┬──────┬──┘ └──────┬───────┘  │              │
  │          ▼      └────────┼──┐   └───────┐   │          │              │
  │  ┌──────────────┐        ▼  │           ▼   ▼          │              │
  │  │ A6 RFI       │  ┌────────┴──┐   ┌──────────────┐    │              │
  │  │    Drafter   │  │           └──►│ A8           ├────┘              │
  │  └───────┬──────┘  │               │ Notification │                   │
  │          │         └──────────────►└──────┬───────┘                   │
  │          ▼                                ▼                           │
  │      ┌────────────────────────────────────────┐                       │
  │      │ A9 Project Memory                      │                       │
  │      └───────────────────┬────────────────────┘                       │
  │                          ▼                                            │
  │      ┌────────────────────────────────────────┐                       │
  │      │ A10 Learning / Predictive              │                       │
  │      └────────────────────────────────────────┘                       │
  └───────────────────────────────────────────────────────────────────────┘
```

The system is **not** implemented as ten separate processes with an agent framework. The graph
describes ten *responsibilities*, and each one maps onto a module that already enforces the
project's single architectural rule:

    Model → Event → Trigger Engine → Rules Engine → Notification → Dashboard

Adding a framework layer on top would add indirection without changing behaviour. What matters is
that every responsibility exists, is isolated, and communicates through the event bus rather than
by calling the next stage directly. This document is the map, and it names the honest gaps.

## Agent → module map

| # | Agent | Implemented by | Notes |
|---|---|---|---|
| **A1** | Vision Ingestion | `core/video_source.py`, `core/vision_engine.py`, `core/pipeline.py` | YOLO11m-pose + BoT-SORT, 17 keypoints, bounded drop-oldest frame queue. Two ingest paths (server camera, browser camera) share one pipeline. |
| **A2** | Measurement | `compliance/calibration.py` | Reference object → px/mm → Euclidean distance and spec deviation. |
| **A3** | Compliance | `rules/engine.py` + the `measurement` event path | Deviation beyond tolerance becomes a `measurement` event; the DB-configurable rules engine decides what compliance failure means (RFI, inspection, alert). |
| **A4** | Hazard / Safety | `safety/fall.py`, `safety/ppe.py`, `safety/proximity.py`, `safety/attention.py`, `inspection/detector.py` | Falls, PPE (5 items), proximity, cognitive-attention state machine, structural damage. |
| **A5** | Voice / NLP | `workforce/questions.py` (text + image) — **voice intake not built** | The worker's "ask a question about this" flow is A5's natural-language half. Speech-to-text from the glasses microphone is deferred with the hardware; see *Deferred* below. |
| **A6** | RFI Drafter | `reasoning/rfi.py` | Deviation → zone-filtered clause retrieval → LLM draft → filed `pending_review`. Citations come from chunk metadata, never from LLM output. |
| **A7** | Knowledge Retrieval | `reasoning/rag.py`, `reasoning/ingest.py`, `reasoning/embeddings.py` | Qdrant with `Filter(must=[FieldCondition…])` on project/zone/category. Serves both A6 and A5. |
| **A8** | Notification | `notifications/service.py`, `broadcast/hub.py`, `alerts/dispatcher.py` | Dedup + retry + channels; WebSocket push; earcons/TTS/haptics. The "TTS Audio Alert" edge of the graph is `alerts/tts.py` + `alerts/haptics.py`. |
| **A9** | Project Memory | `events/store.py`, `backend/store.py`, `storage/docstore.py` | The durable record every other agent reads and writes: events, alerts, RFIs, inspections, zones, feedback, occupancy, questions. Queryable over REST. |
| **A10** | Learning / Predictive | `learning/dataset.py`, `learning/service.py`, `feedback/service.py` | Supervisor feedback → dataset → fine-tune → mAP50 delta on a locked val set → promote only if it did not regress. Predictive risk scoring is `workforce/occupancy.py`'s per-zone risk ranking. |

## Transport edges

| Graph edge | Reality |
|---|---|
| Glasses ↔ Phone (Bluetooth) | **Deferred** — no hardware. |
| Phone → Cloud (WebRTC/RTMP) | **Deferred.** The browser-camera path (`display/server.py` `/ws/video`) stands in for the phone edge node: the operator's browser captures frames and ships JPEG over a WebSocket to the same pipeline. |
| Cloud Ingestion Layer | `backend/app.py` `POST /events` + the event bus (`events/bus.py`, memory or Redis). Any producer — edge pipeline, browser, mobile, a worker's manual report — enters here. |
| A8 → TTS Audio Alert → Phone | `alerts/tts.py` (espeak local, ElevenLabs/Google cloud) and `alerts/haptics.py`. Code paths exist; no keys and no paired device are wired, so local speech plus a logged "simulated buzz" is what actually runs. |
| Multi-Agent System boundary | Not a process boundary. All agents run in the backend service; the bus is the seam. |

## Human roles

The graph shows machine flow. Two human roles consume it:

- **Worker** — sees only their own alerts, checks in and out of a zone, asks A5/A7 a question with a
  photo, and can raise an alert manually (which enters the Cloud Ingestion Layer as a normal event,
  so it flows through A8 like any machine detection).
- **Site manager** — the supervisory view: the alert board, zone occupancy and risk ranking, the RFI
  review queue, the questions inbox, rules, and the live feeds.

A9 (Project Memory) and A10 (Learning) deliberately have **no site-manager UI**. They are
infrastructure: A7 feeds A6's drafting, and A10 consumes the approve/reject decisions the manager
already makes on alerts. Surfacing a training dashboard to a consumer of the system would be noise.

## Deferred, and why

- **Glasses, Bluetooth, WebRTC/RTMP ingest** — needs the Ray-Ban Meta hardware.
- **A5 voice intake (speech-to-text)** — needs the glasses microphone path. `plan.md` correction #1
  already records that the source PRD misidentified Faster-Whisper as text-to-speech; Whisper's
  correct role here is *optional voice-command input*, and it remains unbuilt. The text+image
  question flow covers A5's reasoning half today.
- **LiteRT INT8 export** for on-device inference — edge deployment work.

These are listed as not-started in `tracking.md` rather than stubbed, so nobody mistakes a
placeholder for a working path.
