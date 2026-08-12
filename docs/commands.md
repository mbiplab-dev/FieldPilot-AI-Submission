# Make commands

Every target in the `Makefile`, what it does, and what to expect. `make help` prints a short
version of this list.

Ports used throughout: **:8100** backend REST, **:8000** vision edge, **:3000** dashboard.

Targets marked ✅ were run and verified against a live stack. Targets marked ⏳ are long-running,
destructive, or need hardware/network, and are documented rather than routinely exercised.

---

## Setup

| Target | What it does | |
|---|---|---|
| `make help` | Print every target with its one-line description | ✅ |
| `make setup` | Install everything — backend (uv) and frontend (npm) | ⏳ |
| `make setup-backend` | Python dependencies only, via `uv` | ⏳ |
| `make setup-frontend` | Node dependencies only, via `npm` | ⏳ |
| `make doctor` | Check the local environment: uv, venv, docker, camera, GPU, model weights | ✅ |

`make doctor` is the first thing to run on a new machine. It reports a missing `espeak-ng` as a
neutral note, not a warning — spoken alerts are synthesised on the phone and in the browser, so the
server does not need a speech engine.

## Models and datasets

| Target | What it does | |
|---|---|---|
| `make fetch-models` | Download detector weights into `models/` (`ONLY=pose\|ppe\|damage`) | ⏳ |
| `make val-set-demo` | Generate a **synthetic** demo validation set in `data/val_set` | ⏳ |
| `make audit-ppe DATA=...` | Check labels, class balance, corrupt files, and split leakage | ✅ |
| `make train-ppe DATA=... EPOCHS=60` | Site transfer learning with mAP/recall promotion gates | ⏳ |

The demo val set exists to unblock the mAP50 promotion gate on a machine with no real labelled
data. It is clearly labelled as synthetic — never quote a metric measured against it as a real
accuracy figure.

Use `train-ppe` for a curated site dataset. The older `make train` target below is the incremental
supervisor-feedback loop. See [model-training.md](model-training.md) before using either.

## Infrastructure

| Target | What it does | |
|---|---|---|
| `make infra-up` | Start PostgreSQL, Redis, Qdrant and Ollama via docker compose | ⏳ |
| `make infra-down` | Stop the infrastructure stack | ⏳ |
| `make infra-ps` | Show container status | ✅ |
| `make infra-logs` | Tail infrastructure logs | ⏳ |

The stack runs on SQLite and an in-memory bus without any of this. Postgres/Redis/Qdrant/Ollama are
what you use for a realistic run.

## Running services

| Target | What it does | |
|---|---|---|
| `make backend` | Backend on :8100 — bus, triggers, rules, REST, auth, messaging. API docs at `/docs` | ⏳ |
| `make edge` | Edge safety loop on the webcam, publishing events onto the bus | ⏳ |
| `make edge-synthetic` | Same, on generated frames — no camera needed | ⏳ |
| `make gui` | Live MJPEG view on :8000 with its own camera pipeline | ⏳ |
| `make frontend` | Next.js manager dashboard on :3000 (dev mode) | ⏳ |
| `make frontend-build` | Production build of the dashboard | ⏳ |
| `make frontend-install` | Install frontend dependencies | ⏳ |

## Orchestration

| Target | What it does | |
|---|---|---|
| `make run-all` | Start everything: infra → backend → edge → dashboard. Ctrl-C tears it all down | ⏳ |
| `make stop-all` | Stop everything `run-all` started, including infra | ⏳ |

**A caution worth internalising:** these servers do not hot-reload. If you change Python code, you
must restart the affected service. A stale process that predates your change is the single most
common source of "my fix did nothing" in this project — it has produced 404s on endpoints that
plainly exist in the source, and frames rejected by a decoder that was updated an hour earlier.

## Inspection control

| Target | What it does | |
|---|---|---|
| `make inspect-on` | Turn on structural-damage inspection (toggles the edge detector over the bus) | ✅ |
| `make inspect-off` | Turn it off | ✅ |
| `make inspect-status` | Show the current state | ✅ |

## RAG and the learning loop

| Target | What it does | |
|---|---|---|
| `make llm-pull` | Pull the LLM + embedding models into Ollama | ⏳ |
| `make ingest-blueprints` | Index `data/blueprints/` into Qdrant (`REPLACE=1` rebuilds from scratch) | ⏳ |
| `make blueprints-status` | Show the indexed corpus and embedding backend | ✅ |
| `make blueprints-search Q="rebar spacing" ZONE=zone-a` | Search the specification corpus | ✅ |
| `make train` | Fine-tune on supervisor feedback, gated on the mAP50 delta (`EPOCHS=n`) | ⏳ |
| `make learning-runs` | Fine-tune history with the measured delta per run | ✅ |
| `make feedback-stats` | Supervisor approve/reject counts feeding the loop | ✅ |

`REPLACE=1` on `ingest-blueprints` destroys the existing corpus before rebuilding. `make train`
takes minutes to hours and only promotes new weights if they did not regress.

## Inspecting live state

| Target | What it does | |
|---|---|---|
| `make models` | The detector registry — downloaded, licence, capability | ✅ |
| `make zones` | The site zone registry | ✅ |
| `make rfis` | RFIs awaiting human review | ✅ |
| `make llm-on` | Print how to enable the LLM verification gate (restart the backend after) | ⏳ |

## Seed data

| Target | What it does | |
|---|---|---|
| `make demo-events` | Post sample events — PPE, proximity, crack, measurement | ✅ |

Useful for populating an empty dashboard. Each event is deduplicated by the trigger engine, so
posting twice in quick succession merges rather than producing two alerts.

## Quality and development

| Target | What it does | |
|---|---|---|
| `make test` | Full backend test suite (pytest) | ✅¹ |
| `make test-frontend` | Type-check the frontend (tsc) | ✅¹ |
| `make lint` | ruff (backend) + eslint (frontend) | ✅ |
| `make lint-frontend` | eslint only | ✅ |
| `make validate` | Headless 10-minute synthetic stability run | ⏳ |
| `make bench` | Latency harness — detection→alert, budget < 500 ms | ⏳ |
| `make demo-alert` | Play one sample alert per category (audio/haptics check) | ⏳ |
| `make measure IMAGE=path/to/img.jpg` | Calibrate px→mm from a reference object in an image | ⏳ |
| `make clean` | Remove local DBs, logs, caches and the frontend build (weights kept) | ⏳ |

¹ verified by running the underlying command directly rather than through make.

**`make clean` caution:** it deletes `frontend/.next`. Doing that while the dev server is running
corrupts Turbopack's cache database and the server starts returning 500s. Stop the dev server
first.

## The worker app

The Flutter app has no make targets — drive it with the Flutter CLI from `worker_app/`:

```bash
flutter devices                       # list attached phones
flutter run -d <device-id>            # build, install, run with hot reload
flutter analyze                       # static analysis
flutter test                          # unit + widget tests
export JAVA_HOME=/opt/android-studio/jbr
flutter build apk --debug             # APK build needs a JDK on PATH
```

Adding a native plugin requires a full restart of `flutter run` — hot restart (`R`) only reloads
Dart and will not load new native code.

See [setup.md](setup.md) for connecting the phone to the backend.
