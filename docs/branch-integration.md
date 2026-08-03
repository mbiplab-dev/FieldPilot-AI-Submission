# Integrating the `hrm` branch

`origin/hrm` is an **unrelated history** — it shares no commit with `main`/`biplab`, so there is no
merge base and `git merge` would have to be told to allow unrelated histories. It is a different
take on the same product: a single self-contained FastAPI app (3 Python files + one 58 KB
`dashboard.html`) doing browser-webcam PPE and pose monitoring against a SQLite alert log.

Rather than carrying two apps, its capabilities were ported into this codebase's architecture.
This document records what came across, what did not, and why.

## What `hrm` did better, and is now here

| Capability | Why it mattered | Where it lives now |
|---|---|---|
| **Browser-webcam ingest** | This branch could only read a server-side `/dev/video0`. A site manager on a laptop or phone has the camera attached to their *browser*, not the server. `hrm` shipped frames over a WebSocket instead. | `/ws/video` on the edge app + the `/camera` dashboard page |
| **Multi-model registry with pinned checksums** | This branch hardcoded one `ppe_css.pt`. `hrm` curated four real public PPE checkpoints, each pinned to a git revision **and** a SHA-256, with licence and size declared, downloaded on demand and verified before use. | `fieldpilot/models_registry/`, `GET /models`, `POST /models/select` |
| **Label normalisation across datasets** | Every public PPE dataset names its classes differently (`Hardhat`, `hard_hat`, `NO-Hardhat`, `without_helmet`…). This branch understood two spellings; `hrm` had a full alias table. | `normalise_label()` + `CLASS_ALIASES` in `fieldpilot/safety/ppe.py` |
| **Five PPE items, not two** | helmet, vest, **gloves, boots, goggles** — each with a positive and negative class. | `fieldpilot/safety/ppe.py` |
| **The person-only-model guard** | The one to take seriously. A COCO/person-only detector has no PPE classes, so any "missing helmet" it appears to justify is *fabricated*. `hrm` deliberately paused PPE alerting for such models; this branch did not, and would have invented violations. | `ppe_capable` in `fieldpilot/safety/ppe.py`, surfaced in `/health` |
| **`vest`-without-`no_vest` fallback** | Some datasets have only the positive class. Inferring the violation from "no vest box centre inside this person" recovers the check instead of silently skipping it. | `fieldpilot/safety/ppe.py` |
| **Per-item operator toggles** | Turn goggles checking off on a site that does not require them, without editing YAML. | `safety.tracked_items` defaults + `POST /config/tracked-items` |
| **Runtime confidence / pose tuning** | Same reasoning. | `POST /config/monitoring` |
| **Alert board summary** | totals, today, outstanding, per-item breakdown. | `GET /alerts/stats` |

## Adapted rather than copied

- **Alert acknowledgement.** `hrm` had an `acknowledged` boolean. This platform already tracks an
  alert lifecycle (`NEW → ACTIVE → RESOLVED / SUPPRESSED`) in the trigger engine, so
  `POST /alerts/{id}/acknowledge` resolves the alert instead of adding a second flag that could
  disagree with `state`. "Outstanding" in `/alerts/stats` means NEW or ACTIVE.
- **Settings storage.** `hrm` used its own `settings` / `tracked_items` SQLite tables. Here they
  live in `site_settings` via the shared `storage.docstore`, so the same code path works on
  PostgreSQL, and changes publish `control.settings` on the event bus for the edge to apply —
  matching the existing `control.inspection` pattern.
- **Detection results.** `hrm`'s WebSocket returned detections straight from the model. Here
  browser frames go through the *same* pipeline as camera frames and their hazards are published
  onto the event bus, so they reach triggers → rules → alerts → notifications like any other
  detection. Browser ingest is a camera source, not a parallel system.
- **Model directory.** `hrm` kept weights in `app/models/` and set `YOLO_CONFIG_DIR` as an import
  side effect. Weights here live in the existing repo-root `models/`, and the registry sets no
  global environment variables.

## Deliberately not ported

- **`app/static/dashboard.html`** (58 KB of inline HTML/CSS/JS). This branch has a structured
  Next.js dashboard with 14 routes, typed API client, live WebSocket push and theming. A second,
  unstyled dashboard would be a maintenance liability. The *idea* — a zero-build fallback page —
  is kept as a deliberately small `GET /camera` page on the edge app, so browser capture works
  even with the Next.js app stopped.
- **`hrm`'s flat SQLite schema and per-file `connection()` helper.** Superseded by the event store,
  platform store and docstore, which support both SQLite and PostgreSQL.
- **YOLO26 as the pose backbone.** `hrm` used `yolo26n-pose.pt`; this branch is tuned and
  benchmarked on `yolo11m-pose.pt` (26 ms on the target RTX 3050). YOLO26 detectors are offered in
  the registry as selectable *person* models, so the option is available without changing a
  validated default.

## What this branch has that `hrm` does not

For completeness, since the integration was one-directional: the event bus, trigger engine (dedup /
merge / auto-resolve), DB-configurable rules engine, notification service, zone registry, Qdrant
blueprint RAG with zone isolation, LLM RFI drafting, the feedback → fine-tune → mAP50 gate, offline
store-and-forward, cross-worker zone advisories, the LLM verification gate, fall / proximity /
attention detection, `solvePnP` head pose, measurement calibration, structural-damage inspection,
PostgreSQL + Redis + Qdrant + Ollama infrastructure, and the test suite.
