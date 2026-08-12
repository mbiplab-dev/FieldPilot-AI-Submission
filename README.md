# FieldPilot AI

Real-time, multimodal AI for construction-site safety, spatial compliance, and automated
reporting.

A worker carries a phone. Its camera streams to a laptop that runs every model. When the system
sees a hazard — a missing hard hat, a fall, someone too close to an excavator, rebar spacing out of
tolerance — the worker **hears** it, in the second person, without taking their hands off the
tools:

> *"Stop work. Rebar spacing is 40 millimetres above spec."*

The same hazard reaches the site manager as a different sentence, phrased for someone triaging many
workers at once, alongside the live camera feed it came from.

> ⚠️ **Advisory system.** FieldPilot assists workers. It is not an authoritative safety control and
> must not be relied on as the sole means of hazard detection.

## What it does

**Sees.** Pose-based fall detection, PPE checks, proximity to heavy equipment, an
attention/gaze state machine for hazards a worker has not noticed, and structural-defect
inspection. Measurement against spec uses reference-object calibration, so a deviation is a number
rather than an impression.

**Decides.** Every detection becomes an event and travels one path — event → trigger engine →
rules engine → notification → dashboard. A model never calls a dashboard directly. A local LLM
sits in the middle as noise control, but it is explicitly *not* a safety authority: it can never
silently suppress a high-severity hazard, because a small model was measured binning a
0.97-confidence fall.

**Speaks and listens.** Alerts are spoken on the worker's phone and manager's browser. A worker can
tap the large Pilot beacon and say “Hey FieldPilot” to invoke Android speech recognition, ask the
local Gemma model about a photo, open the calibrated measurement tool, or prepare a hazard report.
Reports still require explicit worker confirmation.

**Files the paperwork.** A spec deviation auto-drafts an RFI, grounded in the site's own
specification documents via retrieval. Citations come from the retrieved chunk's metadata, never
from model output, so a clause number cannot be hallucinated.

**Remembers.** Zone check-in and occupancy, per-zone risk ranking, worker questions answered by the
LLM and then authoritatively by a manager, direct manager↔worker messaging with voice notes, and a
feedback loop that only promotes retrained weights when they did not regress on a locked
validation set.

## The pieces

| | |
|---|---|
| **Worker app** (`worker_app/`) | Flutter. Streams the phone camera, speaks alerts aloud, zone check-in, photo questions, one-tap hazard reports, chat with voice notes |
| **Backend** (`fieldpilot/`) | FastAPI on :8100. Auth, the event chain, alerts, RFIs, messaging |
| **Vision edge** (`fieldpilot/display/`) | FastAPI on :8000. Frame ingest, the detector pipeline, per-worker camera relay |
| **Dashboard** (`frontend/`) | Next.js on :3000. Live worker cameras, alerts with the AI's verdict, questions, messaging, zones, RFIs |

## Documentation

All of it is in [`docs/`](./docs) — start at [`docs/index.md`](./docs/index.md).

- **[Setup and running](./docs/setup.md)** — install, run the services, connect a phone
- **[Hackathon runbook](./docs/hackathon-demo.md)** — preflight, APK install, and three-minute flow
- **[Model training](./docs/model-training.md)** — provenance, dataset audit, and promotion gates
- **[Make commands](./docs/commands.md)** — every target, and which ones are destructive
- **[Architecture](./docs/architecture.md)** — the event chain and why it is inviolable
- **[Pitch vs. code](./docs/pitch-vs-code.md)** — every claim marked built, built-differently, or
  not built

That last one is the honest accounting. Read it before believing anything impressive.

## Status

Built and tested: hazard detection, the full event chain, spoken alerts on both clients, phone
camera streaming with per-worker feeds, grounded RFI drafting, zone occupancy and risk, worker
questions, manager↔worker messaging with voice, offline store-and-forward, and the no-regression
learning gate.

Not built, and deliberately not stubbed: always-listening wake-word hardware, Whisper STT,
monocular depth, BLE indoor positioning, a graph database, 3D reconstruction, and the Procore/BIM
and WhatsApp integrations. The current Android STT is tap-to-arm, and camera measurement requires
a coplanar known-size reference. The impact figures in the pitch deck are projections that nothing
in this repository measures. See
[`docs/pitch-vs-code.md`](./docs/pitch-vs-code.md).
