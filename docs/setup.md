# Setup and running

Everything needed to get FieldPilot running locally: the three services, the dashboard, and a
worker's phone. For what each `make` target does, see [commands.md](commands.md).

## Prerequisites

| Need | Why | Check |
|---|---|---|
| Python 3.12 via [`uv`](https://docs.astral.sh/uv/) | 3.13+ is too new for parts of the ML stack | `make doctor` |
| Node 20+ and npm | The Next.js dashboard | `node -v` |
| Docker + compose | PostgreSQL, Redis, Qdrant, Ollama | `make doctor` |
| Flutter SDK + Android SDK | Only for the worker app | `flutter doctor` |
| An NVIDIA GPU | Optional. CPU inference works, just slower | `make doctor` |

```bash
make setup     # backend (uv) + frontend (npm)
make doctor    # verify the environment before anything else
```

`make doctor` reporting a missing `espeak-ng` is fine. Spoken alerts are synthesised on the phone
and in the browser; the server does not need a speech engine.

## Fastest path

```bash
make fetch-models   # detector weights into models/  (once)
make run-all        # infra → backend :8100 → edge :8000 → dashboard :3000
```

Open <http://localhost:3000> and sign in. Ctrl-C, or `make stop-all`, tears it down.

## Running the pieces separately

Useful when you are changing one service and do not want to restart the others.

```bash
make infra-up          # PostgreSQL + Redis + Qdrant + Ollama
make backend           # :8100 — REST, auth, event chain, messaging.  API docs at /docs
make edge-synthetic    # :8000 — vision pipeline on generated frames (no camera needed)
make frontend          # :3000 — manager dashboard
```

To point the backend at the real infrastructure rather than SQLite and an in-memory bus:

```bash
export FIELDPILOT_EVENTS__BACKEND=postgres
export FIELDPILOT_EVENTS__DATABASE_URL="postgresql+psycopg://fieldpilot:fieldpilot@localhost:5432/fieldpilot"
export FIELDPILOT_EVENTS__EVENTS_DB_URL="postgresql+psycopg://fieldpilot:fieldpilot@localhost:5432/fieldpilot"
export FIELDPILOT_EVENTS__BUS_BACKEND=redis
export FIELDPILOT_EVENTS__REDIS_URL="redis://localhost:6379/0"
```

Any `config.yaml` key can be overridden this way: `FIELDPILOT_<SECTION>__<KEY>`, double underscore
for nesting.

> **Neither service hot-reloads.** After changing Python code you must restart the affected
> service. A process that predates your change is the most common cause of "my fix did nothing" —
> it has produced 404s on routes that plainly exist in the source, and camera frames rejected by a
> decoder that was updated an hour earlier. When something behaves as though your change is absent,
> check the process start time before you debug the code.

## Demo accounts

Seeded on first run, and logged as demo credentials on startup. Change them before any real
deployment.

| Username | Password | Role |
|---|---|---|
| `manager` | `manager123` | site manager |
| `worker1` | `worker123` | worker (`w-1`, Ravi Kumar) |
| `worker2` | `worker123` | worker (`w-2`, Anita Sharma) |

A manager can create further worker accounts from the Workers page.

## Cameras

There are three ways to get frames into the pipeline. All of them run the *same* detectors — the
ingest path never becomes a second, parallel universe.

| Source | How | When to use |
|---|---|---|
| **Worker's phone** | The Flutter app streams raw camera planes to `/ws/video` | The real product path |
| Server camera | `make edge` reads `/dev/video0` | A fixed vantage point, or no phone |
| Browser camera | <http://localhost:8000/camera> | Quick demo with no phone and no server camera |

The phone sends **raw NV21 planes**, not JPEG — the laptop converts them. Encoding on the handset
capped the rate at about 1.4 fps because `takePicture()` is a full still capture; shipping raw
planes reaches the ~10 fps target. The cost is bandwidth: roughly 4.6 MB/s at 480p and 10 fps,
which is fine on a LAN and too heavy for a thin site uplink.

## Connecting a phone

Build and install:

```bash
cd worker_app
flutter devices                      # confirm the phone is visible
flutter run -d <device-id>
```

If the phone does not appear, the usual causes are: USB debugging not enabled, the USB mode set to
"charging only" rather than file transfer, or a charge-only cable. On Xiaomi/MIUI you must also
enable **Install via USB** in Developer options, which requires being signed into a Mi account.

Then set the server address on the app's login screen. Two options:

**Over Wi-Fi** — the phone and laptop on the same network:

```
http://<laptop-lan-ip>:8100
```

Find the address with `ip -4 addr show`. Both services bind `0.0.0.0`, so this works as long as a
firewall is not blocking it:

```bash
sudo ufw allow 8100/tcp    # backend
sudo ufw allow 8000/tcp    # vision edge (camera frames go here)
```

> The edge on :8000 has **no authentication**. While that port is open, anyone on the network can
> list and watch worker camera feeds. Acceptable on a trusted network for development; close it
> when you are done, and do not expose it beyond that.

**Over USB** — no firewall changes, but the tunnel is fragile:

```bash
adb reverse tcp:8100 tcp:8100
adb reverse tcp:8000 tcp:8000
```

Then use `http://localhost:8100`. Both tunnels are needed — the API and the camera are separate
services. `adb reverse` is dropped by any USB renegotiation (screen lock, cable nudge, adb
restart), so if the app suddenly cannot reach the server, re-run those two commands before
debugging anything else.

An Android emulator reaches the host at `http://10.0.2.2:8100`.

## Troubleshooting

| Symptom | Most likely cause |
|---|---|
| "Could not reach the server" in the app | The `adb reverse` tunnel dropped, a firewall, or the laptop's DHCP address changed |
| A camera frame "could not be decoded" | A stale edge process that predates the raw-frame decoder — restart it |
| An endpoint 404s but exists in the source | A stale service process; restart it |
| Dashboard 500s after deleting `.next` | Turbopack's cache was removed under a running dev server; stop it, clear `.next`, restart |
| Alerts appear but are never spoken | Voice is opt-in on the dashboard — arm it with the speaker button in the sidebar |
| No speech on the phone | Media volume, or a device with no TTS engine (the app reports this honestly) |
| Emulator shows no TTS engine | Use a Google APIs / Play image; plain AOSP images often ship without one |

## Configuration

`config.yaml` holds detector thresholds, alert cooldowns, zone definitions, storage paths and LLM
settings. Every key is overridable by environment variable as shown above.
