# FieldPilot AI — Build Tracking

Status legend: ✅ done · 🚧 in progress · ⬜ not started · ⛔ blocked

Last updated: 2026-07-23

## Milestone 0 — Scaffold & deliverables ✅
| # | Task | Status |
|---|------|--------|
| 0.1 | `plan.md` + `tracking.md` at project root | ✅ |
| 0.2 | `uv` project, Python 3.12 venv, `pyproject.toml` | ✅ |
| 0.3 | `config.yaml` — all tunable thresholds | ✅ |
| 0.4 | `.env.example`, `.gitignore`, `README.md` | ✅ |
| 0.5 | `logging_`: structured logger + event schema + SQLite store | ✅ |
| 0.6 | `perspective.py`: EGO/EXO abstraction | ✅ |

## Milestone 1 — Tier-1 edge safety loop (first light) ✅
| # | Task | Status |
|---|------|--------|
| 1.1 | `core`: async video-source abstraction (webcam / file / synthetic) → bounded queue | ✅ |
| 1.2 | `core.VisionEngine`: YOLOv8-Pose + BoT-SORT (`persist`), 17 keypoints | ✅ |
| 1.3 | `safety.FallDetector`: rolling shoulder/hip velocity + torso tilt | ✅ |
| 1.4 | `safety.AttentionStateMachine`: Passive→Unnoticed→Escalated, 600 ms dwell | ✅ |
| 1.5 | `safety.PPE`: pluggable hardhat/vest detector (off until model configured) | ✅ |
| 1.6 | `alerts.Dispatcher`: earcons + TTS (cloud→local) + haptics, cooldowns | ✅ |
| 1.7 | `core`: async event loop; synthetic 15s + webcam verified; SQLite persistence | ✅ |
| 1.8 | Latency harness (detection→alert) | ✅ ~40 ms est. (budget 500 ms) |
| 1.9 | `tests`: fall + attention + config + store + integration (12 passing) | ✅ |

**M1 verified:** GPU inference ~6–7 ms median; 15 s synthetic run 1600+ frames, 0 crashes; webcam
opens (640×480); `ruff` clean; `pytest` 12/12 green.

## Milestone 2 — Streaming, calibration & real multimodal UI
| # | Task | Status |
|---|------|--------|
| 2.1 | `core`: LiveKit/WebRTC ingest, adaptive sampling, resize 1024², BGR→RGBA | ⬜ |
| 2.2 | `models`: LiteRT/TFLite INT8 export + honest memory/latency benchmark | ⬜ |
| 2.3 | `compliance.Calibration`: reference object → px/mm → Euclidean distance | ⬜ |
| 2.4 | `perspective`: full solvePnP→Rodrigues→Euler (exo) + IMU ego path | ⬜ |
| 2.5 | `alerts`: finalized earcon set + Cloud TTS + haptic payload to mobile | ⬜ |

## Milestone 3 — Backend, RAG, RFI, learning, scale
| # | Task | Status |
|---|------|--------|
| 3.1 | `docker-compose.yaml`: PostgreSQL, Redis, Qdrant, Ollama | ⬜ |
| 3.2 | `display`: FastAPI app + Pydantic; feedback (approve/reject + frame + bbox) → PG | ⬜ |
| 3.3 | `learning`: dataset build → fine-tune → mAP50 delta gate (promote if ≥ 0) | ⬜ |
| 3.4 | `reasoning.RAG`: Ollama emb + Qdrant `must`-filter (project/zone/category) | ⬜ |
| 3.5 | `reasoning.RFI`: deviation → LLM draft citing clause → human review queue | ⬜ |
| 3.6 | `alerts.broadcast`: Redis pub/sub → WebSocket zone routing | ⬜ |
| 3.7 | `offline`: SQLite store-and-forward → idempotent reconciled flush | ⬜ |
| 3.8 | `display`: HTMX dashboard (feed, approve/reject, RFI review, mAP delta) | ⬜ |

## Web GUI (pulled forward from M3 display) ✅
| # | Task | Status |
|---|------|--------|
| G.1 | `display/state.py`: thread-safe LiveState (latest JPEG + stats + event feed) | ✅ |
| G.2 | `pipeline.annotate()`: skeleton + boxes + torso tilt + gaze tags + HUD + banner | ✅ |
| G.3 | `display/server.py`: FastAPI MJPEG `/stream` + `/stats` + dark dashboard `/` | ✅ |
| G.4 | `run.py --gui`: launch feed + analysis dashboard at http://localhost:8000 | ✅ |

**GUI verified:** page 200; live stats (person detected, ~30 fps, 8.6 ms infer); `/stream` served
73 valid annotated JPEG frames in a 3 s sample; frames advance in real time.

## Changelog
- **2026-07-23** — Project initialized; plan approved.
- **2026-07-23** — **M0 + M1 complete.** Full offline edge safety loop runs on GPU: YOLOv8-Pose +
  BoT-SORT → fall / attention / PPE → earcon + TTS + haptic → SQLite. Latency ~40 ms (budget 500 ms).
  12 tests passing, lint clean.
- **2026-07-23** — **Web GUI added** (`--gui`): live annotated feed (skeleton, torso tilt, gaze,
  hazard banner) + real-time analysis dashboard + hazard event feed via FastAPI MJPEG. Next: M2
  (LiveKit streaming, LiteRT export, OpenCV calibration).
