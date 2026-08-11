# FieldPilot AI — Build Tracking

Status legend: ✅ done · 🚧 in progress · ⬜ not started · ⛔ blocked

Last updated: 2026-08-03

## Milestone 0 — Scaffold & deliverables ✅
| # | Task | Status |
|---|------|--------|
| 0.1 | `plan.md` + `tracking.md` (now under `docs/`) | ✅ |
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
| 2.1 | `core`: LiveKit/WebRTC ingest, adaptive sampling, resize 1024², BGR→RGBA | ⬜ deferred — edge/wearable work |
| 2.2 | `models`: LiteRT/TFLite INT8 export + honest memory/latency benchmark | ⬜ deferred — edge/wearable work |
| 2.3 | `compliance.Calibration`: reference object → px/mm → Euclidean distance | ✅ `--measure` |
| 2.4 | `perspective`: full solvePnP→Rodrigues→Euler (exo) | ✅ (IMU/ego path deferred with the glasses) |
| 2.5 | `alerts`: finalized earcon set + Cloud TTS + haptic payload to mobile | ◑ cloud TTS + haptics coded; no keys/device wired |

**Deferred by decision (2026-07-30):** LiveKit ingest, LiteRT INT8 export, the IMU ego path and the
battery-drain harness are all *edge-device* work that needs the Ray-Ban hardware. They stay ⬜ on
purpose rather than being faked; everything else in M2/M3 is built and exercised.

## Milestone 3 — Backend, RAG, RFI, learning, scale
| # | Task | Status |
|---|------|--------|
| 3.1 | `docker-compose.yaml`: PostgreSQL, Redis, Qdrant, Ollama | ✅ |
| 3.2 | `feedback`: approve/reject + frame + bbox → PG/SQLite, `POST /alerts/{id}/feedback` | ✅ |
| 3.3 | `learning`: dataset build → fine-tune → mAP50 delta gate (promote if ≥ 0) | ✅ |
| 3.4 | `reasoning.RAG`: Ollama emb + Qdrant `must`-filter (project/zone/category) | ✅ |
| 3.5 | `reasoning.RFI`: deviation → LLM draft citing clause → human review queue | ✅ |
| 3.6 | `broadcast`: bus (Redis) pub/sub → WebSocket zone routing | ✅ |
| 3.7 | `offline`: SQLite store-and-forward → idempotent reconciled flush | ✅ |
| 3.8 | Dashboard (feed, approve/reject, RFI review, mAP delta) | ✅ as Next.js, not HTMX |

## Platform completion pass (2026-07-30)
| # | Task | Status |
|---|------|--------|
| P.1 | `storage.docstore`: one record store over SQLite **and** PostgreSQL | ✅ verified on both |
| P.2 | `zones`: relational registry + `/zones` CRUD; drives rule context, WS routing, RAG filter | ✅ |
| P.3 | `feedback`: decisions + frame + bbox; claim/release so samples are used once | ✅ |
| P.4 | `learning`: real fine-tune, baseline vs candidate mAP50 on the **locked** set, promote-if-≥0 | ✅ ran live |
| P.5 | `reasoning`: PDF/MD ingest → Qdrant; `must`-filtered retrieval; zone isolation proven | ✅ |
| P.6 | `reasoning.rfi`: grounded LLM draft, citations from metadata (never LLM), review queue | ✅ |
| P.7 | `broadcast`: WS `/ws`, dashboards see alerts, devices get zone-scoped advisories only | ✅ |
| P.8 | `offline`: outbox + idempotent replay; zero-loss/zero-dup across a simulated outage | ✅ tested |
| P.9 | PPE weights fetched + loud warning when absent (was silently disabled) | ✅ |
| P.10 | `perspective`: real solvePnP head-pose feeding the attention state machine | ✅ |

**Bugs found and fixed during this pass** (all pre-existing):
- `events.schema.dedup_key()` ignored `element`/`ppe_item`/`defect` despite its docstring promising
  them, so two unrelated issues in one zone collapsed into one alert and the second never reached
  the rules engine.
- `perspective` computed the bearing to a hazard with a 1-pixel focal length, saturating to ±90°
  for any offset.
- `offline.Outbox` coerced timestamps with `float()`, which crashed on the ISO-8601 string that
  `Event.model_dump_json_safe()` actually produces — the edge's forward path would have failed on
  its first event.
- `feedback` defaulted a sample's label to the event *family* (`"ppe"`), which never matches a YOLO
  class, so every approval without an explicit label was silently dropped by the dataset builder.
- `broadcast`: a device connecting without a zone received every zone's advisories — the exact
  site-wide noise §4.4 exists to prevent.
- `ppe.PPEChecker` swallowed load failures silently; `zones.update` skipped the name validation
  `create` performs; `docstore` could not enforce immutability for undeclared columns.
- **The LLM gate suppressed real hazards.** Running live, `llama3.2:3b` with `vision: false`
  rejected 23 of 24 alerts — 14 of 19 rejections citing visual evidence ("no clear visual evidence
  in the bounding box") for a frame a text-only model was never given — and binned a
  0.97-confidence fall described as "worker collapsed and is motionless". The gate is now
  constrained: **no vision → no veto**, and even with vision it cannot suppress above
  `llm.suppress_max_severity`. Rejections above the line are persisted as
  `llm_verdict.disputed` and still escalate for a human. Re-verified live: 42 alerts suppressed
  before the change, 0 after, with critical falls staying live and flagged.

## Live full-stack verification (2026-08-03)
`make run-all` with PostgreSQL + Redis + Qdrant + Ollama, real webcam, LLM gate on:

| Surface | Result |
|---|---|
| Edge pipeline | 14–23 fps, 29–45 ms inference, real PPE + attention hazards on the webcam |
| MJPEG `/stream` | 83 distinct JPEG frames in a 4 s sample |
| Edge → Redis bus → backend | live `ppe` events → dedup (hits=3) → alerts stamped `zone-a` |
| LLM gate | verdicts recorded per alert; timeouts fail open (GPU shared with 3 vision models) |
| Alert snapshots | `/images/<id>.jpg` 200 (45 KB) via backend **and** `/img/` frontend proxy |
| Blueprint RAG | 3 docs indexed, real `nomic-embed-text`; zone-a→clause 3.4.2, zone-b→clause 7.1, no leak |
| Grounded RFI | drafted per zone citing its own clause, `pending_review`, approve/reject working |
| Learning gate | real fine-tune: 12 samples → mAP50 0.1226 → 0.1283 (Δ +0.0057) → promoted, weights written |
| WebSocket | dashboard receives alerts; zone-a device receives the downgraded advisory; zone-b silent |
| Offline outbox | zero dropped, zero duplicated across a simulated outage; timestamps reconciled |
| Inspection toggle | `POST /control/inspection` → bus → edge switched the damage detector at runtime |
| Dashboard | 10 routes served 200 (`/ /alerts /rfis /zones /learning /blueprints /live /workers /rules /activity`) |
| Tests / lint | 293 passing, `ruff` clean, frontend `lint` + `tsc` + `build` clean |

## Event-driven platform (Model → Event → Rules → Notification → Dashboard) ✅
| # | Task | Status |
|---|------|--------|
| E.1 | `events.schema`: canonical Event (11 spec fields, 9 event types) | ✅ |
| E.2 | `events.bus`: in-memory + Redis pub/sub backends, pattern topics | ✅ |
| E.3 | `events.store`: durable event log — PostgreSQL / SQLite, one interface | ✅ |
| E.4 | `triggers.engine`: dedup 45 s, merge, NEW/ACTIVE/RESOLVED/SUPPRESSED, auto-resolve sweeper | ✅ |
| E.5 | `triggers.cache`: Redis / in-memory hot cache | ✅ |
| E.6 | `rules.engine`: DB-configurable rules, condition DSL, templated actions, cooldowns | ✅ |
| E.7 | `notifications.service`: dedup + channels (dashboard/sms/email/whatsapp/push) + retry | ✅ |
| E.8 | `backend.app`: REST — /events /alerts /rules /workers/{id}/timeline /notifications /rfis /inspections | ✅ |
| E.9 | `events.bridge`: edge pipeline publishes HazardEvents onto the bus (`--bus`) | ✅ |
| E.10 | 41 new tests (schema, bus, triggers, rules, E2E flow, REST API) — 60 total green | ✅ |

**Verified live:** proximity + PPE events → rule `no-helmet-in-danger-zone` fired critical
notification; measurement event (rebar 27.5 mm) → RFI auto-generated; 500 dup/min → 1 alert.

## Web GUI (pulled forward from M3 display) ✅
| # | Task | Status |
|---|------|--------|
| G.1 | `display/state.py`: thread-safe LiveState (latest JPEG + stats + event feed) | ✅ |
| G.2 | `pipeline.annotate()`: skeleton + boxes + torso tilt + gaze tags + HUD + banner | ✅ |
| G.3 | `display/server.py`: FastAPI MJPEG `/stream` + `/stats` + dark dashboard `/` | ✅ |
| G.4 | `run.py --gui`: launch feed + analysis dashboard at http://localhost:8000 | ✅ |
| G.5 | `frontend/`: Next.js site-manager dashboard (:3000) — dark/light, 6 pages, inspection toggle | ✅ |
| I.1 | `inspection.detector`: fine-tuned `structural_damage_best.pt` → `crack` events | ✅ |
| I.2 | `POST /control/inspection` → bus → edge toggles detector at runtime | ✅ |

**GUI verified:** page 200; live stats (person detected, ~30 fps, 8.6 ms infer); `/stream` served
73 valid annotated JPEG frames in a 3 s sample; frames advance in real time.

## Capability expansion (possibilities.md) ✅
| # | Task | Status |
|---|------|--------|
| C.1 | Upgrade pose backbone → **YOLO11m-pose** (n/s/m/l selectable) | ✅ |
| C.2 | **Real PPE detection** (10-class construction model) + violation alerts | ✅ |
| C.3 | **Proximity / danger-zone** (worker near machinery/vehicle) | ✅ |
| C.4 | **Equipment/vehicle** detection overlay | ✅ |
| C.5 | Live **fall-risk meter** per worker + tuned thresholds | ✅ |
| C.6 | `docs/possibilities-coverage.md` — full mapping of the 18 viso.ai apps | ✅ |

**Verified live:** GUI fired `ppe_missing`, `unnoticed_hazard`, and `proximity` alerts on the webcam
(hazards=42 in one sample; alerts 34–37 ms); YOLO11m 26 ms; ~16 fps combined pipeline.

## Changelog
- **2026-07-23** — Project initialized; plan approved.
- **2026-07-23** — **M0 + M1 complete.** Full offline edge safety loop runs on GPU: YOLOv8-Pose +
  BoT-SORT → fall / attention / PPE → earcon + TTS + haptic → SQLite. Latency ~40 ms (budget 500 ms).
  12 tests passing, lint clean.
- **2026-07-23** — **Web GUI added** (`--gui`): live annotated feed (skeleton, torso tilt, gaze,
  hazard banner) + real-time analysis dashboard + hazard event feed via FastAPI MJPEG.
- **2026-07-23** — **Capability expansion + M2 start.** Upgraded to YOLO11m-pose; added real PPE
  detection, proximity/danger-zone, equipment overlay, fall-risk meter; measurement/calibration
  (`--measure`). Mapped possibilities.md (18 apps) in docs. 19 tests passing. Git repo initialized;
  committing per major change. Next: LiteRT INT8 export, LiveKit streaming, then M3 (RAG + learning).
- **2026-07-26** — **Event-driven platform backbone.** Canonical event schema, event bus
  (memory/Redis), durable event store (PostgreSQL/SQLite), intelligent trigger engine (45 s dedup,
  merge, auto-resolve), DB-configurable rules engine, notification service (dedup + channels +
  retry), full REST backend, and the edge→bus bridge (`--bus`). `docker-compose.yaml` for
  PG/Redis/Qdrant/Ollama. 60 tests green, ruff clean.
- **2026-07-26** — **Make-based project setup.** `Makefile` (setup / infra / run / orchestrate /
  quality targets, `make help` self-docs), `scripts/run_all.sh` — one command starts infra +
  backend + edge + frontend with trap-based clean shutdown; `scripts/stop_all.sh`,
  `scripts/doctor.sh` environment checker.
- **2026-07-27** — **Inspection AI + Next.js dashboard.** Wired the fine-tuned
  `structural_damage_best.pt` (Minor/Moderate/Severe rotation) as a toggleable inspection
  detector in the edge pipeline — `POST /control/inspection {enabled:true}` flows over the
  bus (`control.inspection`) to the edge; detections become `crack` events → rules engine →
  immediate inspection request (severity > 0.85). Professional Next.js 16 dashboard
  (`frontend/`) with dark/light mode, sidebar nav, Overview/Live/Alerts/Workers/Rules/Activity
  pages; live MJPEG feed; inspection-mode toggle switch. `make run-all` starts everything:
  infra (pg+redis) → backend (:8100) → edge feed+bus (:8000) → dashboard (:3000).
  68 tests green; backend + frontend lint clean.
- **2026-07-27** — **LLM verification gate.** Every alert now hops through a locally-runnable
  LLM (Ollama `llama3.2:3b`) for a final verdict before notifying anyone. The edge captures an
  annotated snapshot (bbox + label) per trigger → saved to `data/alerts/` → served at
  `/images/<id>.jpg`. The LLM sees the event metadata (+ image when vision is on) and returns
  `{confirmed, confidence, reasoning, severity}`; rejected alerts are auto-suppressed.
  *(Superseded 2026-08-03 — auto-suppression proved unsafe in practice and is now gated on the
  verifier having vision and the severity being at or below `llm.suppress_max_severity`.)*
  Fail-open: LLM-unavailable → auto-confirm. Alerts page rebuilt as **image cards** with the
  snapshot, severity/state chips, and an LLM verdict badge (confirmed/rejected). `make llm-pull`
  fetches the model; enable via `FIELDPILOT_LLM__ENABLED=true`. 74 tests green.
- **2026-08-03** — **Platform completion pass.** Everything in M2/M3 that does not require the
  Ray-Ban hardware is now built, wired and exercised on real infrastructure: a shared
  SQLite/PostgreSQL record store, a relational zone registry, the supervisor feedback loop, the
  fine-tune + mAP50 gate (ran live, promoted on a measured +0.0057 delta), Qdrant blueprint
  retrieval with proven zone isolation, grounded LLM RFI drafting into a human review queue,
  WebSocket live push with zone-scoped cross-worker advisories, and the offline store-and-forward
  outbox (zero dropped / zero duplicated across a simulated outage). Real PPE weights fetched;
  `solvePnP` head-pose replaced the yaw heuristic. Frontend extended to 13 routes covering every
  endpoint, with live push and supervisor approve/reject. Seven pre-existing bugs fixed, including
  an LLM gate that was silently suppressing critical hazards. 293 tests, ruff + eslint + tsc clean.
  Still deliberately deferred to the glasses: LiveKit ingest, LiteRT INT8 export, the IMU ego
  path, and the battery-drain harness.

## `hrm` branch integration (2026-08-03)
`origin/hrm` is an unrelated history (no merge base) — a 3-file self-contained browser-webcam app.
Its capabilities were ported into this architecture rather than carried as a second app; see
[`docs/branch-integration.md`](./docs/branch-integration.md) for the full accounting.

| # | Ported capability | Status |
|---|---|---|
| H.1 | `models_registry`: 4 pinned public PPE checkpoints + person models + custom slot, SHA-256-verified atomic download, licence/size metadata | ✅ 9 entries, digests re-verified against upstream LFS |
| H.2 | Label normalisation (`CLASS_ALIASES`) so any PPE dataset's spelling works | ✅ |
| H.3 | Five-item PPE vocabulary — helmet, vest, gloves, boots, goggles | ✅ |
| H.4 | **Person-only-model guard** — PPE alerting is suppressed when the loaded model has no PPE classes | ✅ closes a real safety gap |
| H.5 | `vest`-without-`no_vest` person-containment fallback | ✅ |
| H.6 | Browser-webcam ingest: `/ws/video` + `/camera` fallback page + dashboard Camera page | ✅ verified live |
| H.7 | Operator settings: per-item PPE toggles, confidence/pose tuning, detector selection — persisted, override YAML, pushed to the edge over the bus | ✅ verified live |
| H.8 | Alert board summary + acknowledge | ✅ `GET /alerts/stats` |

**Not ported, deliberately:** `hrm`'s 58 KB inline `dashboard.html` (superseded by the Next.js app;
the *idea* survives as the small zero-build `/camera` page), its flat SQLite schema, and YOLO26 as
the pose default (offered in the registry as a selectable person model instead of replacing a
benchmarked default).

**Bugs found and fixed while integrating:**
- `set_tracked_items` resolved a payload of unrecognised names to the empty set, **silently
  disabling every PPE check** — the most dangerous reading of "I could not understand you". It now
  keeps the current selection unless the all-off request is explicit.
- The `/models/select` endpoint wrapped the registry's coroutine `ensure_weights` in
  `run_in_threadpool`, which returns an un-awaited coroutine that stringifies into a bogus weights
  path instead of downloading anything. Awaited directly, with tests over the download path.

**Verified live** on the running stack: `/camera` fallback served (8.1 KB, real capture JS); browser
frames processed at 27–65 ms after a 556 ms lazy model load; an undecodable payload reported without
dropping the socket; browser hazards reach the platform bridge tagged `ingest: "browser"`; a `gloves`
toggle and a confidence change propagated backend → bus → live edge detector; `/alerts/stats`
reporting 132 alerts (108 today, 2 outstanding, 88 resolved, 42 suppressed, 6 LLM-disputed).
424 tests, ruff clean, frontend lint + tsc + build clean (14 routes).
