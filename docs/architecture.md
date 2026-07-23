# FieldPilot AI — Architecture (Milestone 1)

## Data flow

```
VideoSource (thread)          Pipeline (async loop)                     Alerts (thread pool)
  webcam/file/synthetic  ─▶   engine.infer(frame)  ─▶  safety detectors ─▶ dispatch ─▶ earcon
  bounded drop-oldest Q       (executor, GPU)          fall / ppe / attn    (cooldown)   + TTS
                                    │                         │                          + haptic
                                    ▼                         ▼
                              FrameResult              HazardEvent ──▶ SQLite store (+ JSONL)
```

Capture runs on its own thread because OpenCV `VideoCapture` is blocking. Frames land in a bounded
queue with **drop-oldest** semantics, so under GPU backpressure we keep the freshest frame instead of
growing latency. Inference runs in a thread executor (torch releases the GIL during CUDA ops), so it
never stalls capture. Alert side-effects (audio + speech) run on a separate small thread pool so a
slow TTS call can't block the inference loop.

## Components

| Module | Responsibility |
|--------|----------------|
| `core/video_source.py` | Threaded capture → async bounded queue (webcam / file / synthetic). |
| `core/vision_engine.py` | YOLOv8-Pose + BoT-SORT (`persist=True`); `track_buffer`/`match_thresh` injected via a cloned tracker config. |
| `core/pipeline.py` | Orchestrates capture → infer → detect → alert → store; tracks latency; keeps hazards "active" for attention scoring. |
| `perspective.py` | EGO/EXO gaze abstraction; supplies `gaze_fn(person, bbox) -> bool`. |
| `safety/fall.py` | Rolling shoulder/hip velocity **and** torso tilt → fall vs kneel/squat. |
| `safety/attention.py` | Per-(worker, hazard) Passive→Unnoticed→Escalated (600 ms dwell). |
| `safety/ppe.py` | Optional, separate hardhat/vest detector (pluggable; off by default). |
| `alerts/*` | Earcons (synthesized), pluggable TTS (cloud→local), haptics, dispatcher w/ cooldowns. |
| `logging_/store.py` | SQLite event store; idempotent by event id; doubles as the offline queue. |

## Measured performance (RTX 3050 6GB)

- YOLOv8n-pose inference: **~6–7 ms median** per frame at 640².
- End-to-end detection→alert estimate: **~40 ms** — well within the 500 ms budget.
- 15 s synthetic stress run: 1600+ frames, zero crashes, drop-oldest confirmed under backpressure.

## Honest notes / where M1 is deliberately coarse

- **Gaze in EXO mode is a coarse head-yaw heuristic** from the 5 facial keypoints. Milestone 2
  replaces it with full `solvePnP` head-pose (Rodrigues → Euler yaw/pitch/roll).
- **EGO gaze uses the optical-axis proxy** (the wearer looks where the camera points). Real yaw/pitch
  arrives with the glasses/phone IMU via `GazeEstimator.set_orientation()`.
- **PPE detection is off unless a model is configured** — YOLOv8-Pose does not detect PPE, so this
  needs a separate detector at `detection.ppe_model`.
- **Fall/attention operate on observed (exo) workers.** A wearer cannot see their own fall from their
  own glasses; that path will fuse the IMU when the hardware lands.
