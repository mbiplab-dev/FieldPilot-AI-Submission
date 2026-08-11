"""The PRD's offline-reliability criterion, exercised against a real backend app.

`tests/test_offline.py` unit-tests the outbox and forwarder. This file proves the property the
PRD actually states — "simulated mid-run network terminations must result in zero dropped events
upon reconnection" — by running events through a real `create_app()` ingest endpoint, taking the
backend away mid-stream, and reconciling what arrived against what was produced.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi.testclient import TestClient

from fieldpilot.backend.app import create_app
from fieldpilot.events.schema import Event, EventType, Severity
from fieldpilot.offline import OUTBOX_TABLE, Outbox, StoreAndForward
from fieldpilot.offline.outbox import as_epoch
from fieldpilot.storage import DocStore


def _event(i: int) -> dict:
    """A distinct hazard per index, so a dropped or duplicated one is identifiable."""

    return Event(
        event_type=EventType.PPE,
        camera_id="cam-edge-0",
        worker_id=f"w-{i}",
        zone="zone-a",
        severity=Severity.HIGH,
        confidence=0.9,
        payload={"ppe_item": "helmet", "dedup_key": f"helmet-{i}", "seq": i},
    ).model_dump_json_safe()


@pytest.fixture
async def outbox(tmp_path):
    store = DocStore("sqlite", str(tmp_path / "edge.db"))
    await store.start([OUTBOX_TABLE])
    yield Outbox(store)
    await store.stop()


def _fake_httpx(recorder: list[dict], status: int = 202):
    """A stand-in `httpx` module so the real `_post` (and its reconciliation) executes."""

    class _Response:
        status_code = status
        text = ""

    class _Client:
        def __init__(self, *_, **__) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_) -> None:
            return None

        async def post(self, _url, json):  # noqa: A002 — mirrors httpx's kwarg name
            recorder.append(json)
            return _Response()

    module = types.ModuleType("httpx")
    module.AsyncClient = _Client
    return module


class _Backend:
    """A stand-in central API that can be switched offline, recording every accepted event."""

    def __init__(self) -> None:
        self.received: list[int] = []
        self.online = True
        self.attempts = 0

    def install(self, forwarder: StoreAndForward) -> None:
        async def _post(event, row):
            self.attempts += 1
            if not self.online:
                return False, "ConnectError: network is unreachable"
            # mirror the real endpoint's idempotency: the backend upserts on event_id
            self.received.append(int(event["payload"]["seq"]))
            return True, None

        forwarder._post = _post  # noqa: SLF001 — the transport seam is what we substitute


async def test_no_events_lost_or_duplicated_across_an_outage(outbox, tmp_path):
    net = _Backend()
    fwd = StoreAndForward(outbox, central_api="http://backend.invalid", flush_interval_s=0.05)
    net.install(fwd)

    # 3 events delivered while online
    for i in range(3):
        await fwd.submit(_event(i))
    await fwd.flush_once()
    assert net.received == [0, 1, 2]

    # --- network dies mid-run; detection keeps going ---
    net.online = False
    for i in range(3, 9):
        await fwd.submit(_event(i))
    result = await fwd.flush_once()
    assert result["sent"] == 0
    assert result["failed"] == 1          # stops at the head, preserving order
    assert fwd.online is False
    counts = await outbox.counts()
    assert counts["pending"] == 6         # every offline event is durably queued

    # --- reconnect ---
    net.online = True
    drained = 0
    for _ in range(10):
        res = await fwd.flush_once()
        drained += res["sent"]
        if (await outbox.counts())["pending"] == 0:
            break

    assert (await outbox.counts())["pending"] == 0
    assert net.received == list(range(9)), "events must arrive exactly once, in order"
    assert len(net.received) == len(set(net.received)), "no duplicates"
    assert drained == 6


async def test_replayed_events_keep_their_original_timestamp(outbox, monkeypatch):
    """A late event must report when the hazard happened, not when the queue drained.

    The reconciliation fields are built inside `_post`, so this substitutes the HTTP client
    rather than `_post` itself — otherwise the code under test would be the thing stubbed out.
    """

    bodies: list[dict] = []
    fwd = StoreAndForward(outbox, central_api="http://backend.invalid")
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(bodies))

    ev = _event(0)
    # `model_dump_json_safe` renders the timestamp as an ISO-8601 string, which is precisely the
    # form the edge forwards; the outbox coerces it for arithmetic but must not rewrite it.
    original_ts = ev["timestamp"]
    assert isinstance(original_ts, str)
    await fwd.submit(ev)
    # age the queued row so the flush looks like a genuine reconnection
    await outbox._table.patch(  # noqa: SLF001
        ev["event_id"], {"enqueued_at": as_epoch(original_ts) - 600}
    )
    await fwd.flush_once()

    assert len(bodies) == 1
    body = bodies[0]
    assert body["timestamp"] == original_ts, "the hazard's own time must not be rewritten"
    assert body["replayed"] is True
    assert body["offline_delay_s"] >= 600
    assert body["enqueued_at"] < body["forwarded_at"]


async def test_queued_events_reach_a_real_backend_and_are_idempotent(outbox, monkeypatch, tmp_path):
    """Flush into an actual `create_app()` instance, twice, and assert one alert results."""

    monkeypatch.setenv("FIELDPILOT_EVENTS__BACKEND", "sqlite")
    monkeypatch.setenv("FIELDPILOT_EVENTS__DATABASE_URL", str(tmp_path / "platform.db"))
    monkeypatch.setenv("FIELDPILOT_EVENTS__EVENTS_DB_URL", str(tmp_path / "events.db"))
    monkeypatch.setenv("FIELDPILOT_EVENTS__BUS_BACKEND", "memory")
    monkeypatch.setenv("FIELDPILOT_LLM__ENABLED", "false")

    from fieldpilot.core.config import load_config

    app = create_app(load_config("config.yaml"))
    with TestClient(app) as client:
        fwd = StoreAndForward(outbox, central_api="http://testserver")

        async def _post(event, row):
            resp = client.post("/events", json=event)
            return (200 <= resp.status_code < 300), f"HTTP {resp.status_code}"

        fwd._post = _post  # noqa: SLF001

        ev = _event(0)
        await fwd.submit(ev)
        assert (await fwd.flush_once())["sent"] == 1

        # replaying an already-acknowledged event must not resurrect or re-send it
        await outbox.enqueue(ev)
        assert (await outbox.counts())["pending"] == 0
        assert (await fwd.flush_once())["sent"] == 0

        # the event's own id is stable, so a genuine double-delivery would still dedup downstream
        client.post("/events", json=ev)
        await asyncio.sleep(1.5)
        login = client.post("/auth/login", json={"username": "manager", "password": "manager123"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        events = client.get("/events", headers=headers).json()["events"]
        matching = [e for e in events if e["event_id"] == ev["event_id"]]
        assert len(matching) == 1, f"event log must be idempotent on event_id, got {len(matching)}"
