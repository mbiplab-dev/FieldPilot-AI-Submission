# The pitch deck, claim by claim, against the code

`FieldPilot_AI_Pitch_Deck.pptx` is the story. This file is the audit: for every claim on every
slide, what actually runs, what runs differently, and what is not built. It exists so nobody —
including us — has to guess which is which in front of a judge.

Verified against the running stack (FastAPI backend on Postgres + Redis + Qdrant + Ollama, Next.js
dashboard, Flutter worker app) on 2026-08-05.

Legend: **✅ built** · **◐ built differently** · **⭕ not built**

---

## Slide 3 — the solution

| Claim | Status | Reality |
|---|---|---|
| "Meta smart glasses stream what the worker sees" | ⭕ | No glasses SDK integration. The browser-camera path (`display/server.py` `/ws/video`) stands in as the capture source. |
| "10 agents do the work" | ✅ | All ten responsibilities exist as modules. Not ten processes with an agent framework — see [agents.md](agents.md) for the module-by-module map and why. |
| "auto-draft the RFI with the spec quoted" | ✅ | `reasoning/rfi.py`. Citations come from retrieved chunk metadata, never from LLM output, so a clause number cannot be hallucinated. |
| **"The worker hears the verdict … 'Stop work — rebar spacing 40mm above spec'"** | ✅ | Implemented, and verified end to end. The exact sentence is what the worker's phone speaks. See *Spoken alerts* below. |
| "predicts the next failure" | ◐ | Per-zone risk ranking from historical warning counts (`workforce/occupancy.py`). Real, but statistics — not the learned predictive model the deck implies. |

## Spoken alerts — how this actually works

The deck's headline promise. Worth its own section because the implementation is deliberately not
what you would guess.

The backend does have `alerts/tts.py` (espeak / ElevenLabs / Google), but it renders onto the
**server's** speakers — a machine nobody on a site is standing next to. On this host it cannot speak
at all: espeak-ng is not installed and pyttsx3 needs it, so every call fell through to an
`all TTS backends failed` log line.

So the *sentence* is the data. `alerts/speech.py` writes one hazard three ways, and each client
synthesises locally — no API key, no server audio stack, and it keeps working in a dead zone:

| Audience | Hears | Why different |
|---|---|---|
| the worker at risk | *"Stop work. Put on your hard hat."* | Second person, imperative. No ids or zone names read back — they are standing in it. |
| a colleague in the zone | *"Heads up. A worker is missing a hard hat in your zone."* | Warned without a colleague's id spoken aloud (PRD §4.4). |
| the site manager | *"High alert. Worker w-9 is missing a hard hat in zone A12."* | Fully qualified — they triage across many workers and zones. |

Delivery: the hub addresses the worker's own device by `worker_id` (`alert_worker`), excludes that
worker from the peer advisory so nobody is told twice, and never sends a personal alert to a
colleague. A worker in another zone hears nothing. Verified with four live sockets against Redis.

Peer advisory speech is derived from the **original** severity. §4.4 downgrades an advisory's
delivery priority to prevent alert storms; it is not a claim the hazard shrank, and
*"Note: fire in your zone"* would fail in the dangerous direction.

Both clients share one policy: dedup by key (the same alert arrives by socket *and* by poll),
critical/high interrupt whatever is being said, medium/low drop rather than queue behind a stale
sentence. Voice defaults **on** in the phone app and **off** on the dashboard — a worker with the
phone in their pocket is the intended listener; a manager in a shared office is not.

⭕ **Not built:** audio playback *through the glasses' speaker*. The phone speaks. Routing that to
open-ear glasses audio is a Bluetooth pairing job that needs the hardware.

## Slide 5 — tech stack

| Deck says | Status | Actually |
|---|---|---|
| Meta smart glasses; RealWear / Vuzix fallback | ⭕ | No device integration. |
| BLE beacons — zone tracking | ⭕ | Replaced by explicit zone check-in/out in the worker app. Less magical, but it works today and is auditable. |
| Snapdragon / Apple NPU | ⭕ | Edge/on-device inference deferred. |
| YOLOv9-tiny | ◐ | **YOLO11m-pose** (17 keypoints) + BoT-SORT tracking. |
| YOLOv9 / RT-DETR | ◐ | Same as above. |
| Llama.cpp 3B / Llama-3 8B | ◐ | **llama3.2:3b via Ollama**. |
| LLaVA / Qwen-VL | ◐ | Config supports a vision model (`llm.vision`), default off. The gate is **noise control, not a safety authority**: text-only it may suppress nothing, and even with vision it cannot suppress above a severity ceiling — a 3B model was measured binning a 0.97-confidence fall. |
| Depth Anything V2 (monocular depth) | ⭕ | Measurement is **reference-object px/mm calibration** (`compliance/calibration.py`), not a depth network. The deck's `Z = fB/d` formula is stereo disparity and does not describe what runs. |
| WebRTC / RTMP streaming | ⭕ | JPEG frames over a WebSocket. |
| NeRF / Gaussian Splatting | ⭕ | Not built. No 3D reconstruction anywhere. |
| Whisper (speech-to-text) | ⭕ | Not built. The worker asks questions by **text + photo**, which covers Agent 5's reasoning half; voice intake is deferred with the microphone. |
| Neo4j knowledge graph | ⭕ | Project memory is the relational store (`storage/docstore.py` over SQLite/Postgres). No graph database. |
| RAG spec retrieval | ✅ | Qdrant with hard `Filter(must=[FieldCondition…])` isolation on project/zone/category. |
| BIM / Procore integration | ⭕ | Not built. |
| 10-agent orchestration | ✅ | See [agents.md](agents.md). |
| Web command dashboard | ✅ | Next.js dashboard, manager-gated. |
| WhatsApp alerts | ⭕ | `whatsapp` is in the severity→channel routing table but has **no registered sender**, so it persists as `status: "skipped"` for audit rather than pretending to send. |
| TTS alerts | ✅ | See above. |

## Slide 6 — models and fine-tuning

The base models named here (Depth Anything V2, LLaVA, Whisper, RT-DETR) are **not** what runs; see
the table above. The fine-tuning column describes a plan, not completed training. What does exist is
the *loop* that would consume it: supervisor feedback → dataset → train → mAP50 on a locked
validation set → promote only if it did not regress (`learning/`). The demo validation set is
generated and clearly labelled as such.

## Slide 7 — the product

✅ The dashboard is real and live: alerts, RFI review queue, worker questions inbox, zone occupancy
and risk ranking, rules, workers, activity, live feeds, browser camera.

⭕ **Not built:** the second "Executive" dashboard, ROI calculators, and the Live 3D site map. There
is one dashboard, not two.

◐ Live updates are **WebSockets**, not Server-Sent Events as the deck states.

## Slide 8 — impact numbers

⭕ 23 RFIs avoided/month · $1.3M saved · 340h saved · 94.3% accuracy — these are **projections, not
measurements**. Nothing in this repo measures them. There has been no site pilot. The one number
the system does produce honestly is detector mAP50 on the locked validation set, from `learning/`.

Present them as a model of expected value, never as observed results.

## Slide 9 — roadmap and risk

Mapped honestly against the phases the deck defines:

- **Phase 1 (MVP)** — ✅ largely done: capture, hazard detection, the full event→alert→notify chain.
- **Phase 2 (Edge & Scale)** — ◐ partial. **Offline mode is built** (store-and-forward outbox, and
  it is tested). Edge/NPU optimisation and LiteRT INT8 export are not.
- **Phase 3 (Enterprise)** — ⭕ no Procore/BIM 360 integration; not tested at 400-worker scale.
- **Phase 4 (Intelligence)** — ⭕ predictive RFI is zone risk statistics, not a learned model.

The risk-mitigation column is the most honest part of the deck. Three of five mitigations are
genuinely implemented: offline-first with auto-sync, role-based access, and continuous
retraining + human oversight (the LLM gate can never silently bin a high-severity hazard).

## Slide 10 — resources

⭕ The Vercel demo URL is not this stack. This runs locally: backend `:8100`, dashboard `:3000`, edge
`:8000`, and the Flutter app on a device pointed at the backend's LAN address.

---

## Summary

**Real and verified:** the ten-responsibility architecture and its one inviolable chain
(Model → Event → Trigger → Rules → Notification → Dashboard); hazard detection (fall, PPE,
proximity, attention, structural); reference-object measurement; grounded RFI drafting with
un-hallucinatable citations; Qdrant RAG with zone isolation; worker/manager auth; zone occupancy and
risk ranking; photo questions answered by LLM then authoritatively by a manager; manual hazard
reports that enter the same chain as machine detections; offline store-and-forward; the learning
loop with a no-regression promotion gate; **and spoken alerts on both the phone and the dashboard.**

**The honest gaps, in order of how much they matter to the story:** no glasses (so no hands-free
capture and no open-ear audio), no Whisper voice intake, no monocular depth network, no BLE
localisation, no Neo4j, no NeRF, no WhatsApp, no Procore/BIM, one dashboard rather than two, and the
impact figures are projections.

None of the gaps are stubbed to look finished. `tracking.md` lists them as not-started so a
placeholder is never mistaken for a working path.
