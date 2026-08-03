"""Store-and-forward: the PRD's "zero dropped events" guarantee.

The network is replaced by a fake `httpx` module injected into `sys.modules`, so the real
`StoreAndForward._post` (and its timestamp reconciliation) runs unmodified while the test
decides exactly which posts succeed.
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

from fieldpilot.offline.forwarder import StoreAndForward
from fieldpilot.offline.outbox import OUTBOX_TABLE, Outbox
from fieldpilot.storage import DocStore

CENTRAL = "http://central.invalid/api"


@pytest.fixture
async def outbox(tmp_path):
    store = DocStore("sqlite", str(tmp_path / "outbox.db"))
    await store.start([OUTBOX_TABLE])
    try:
        yield Outbox(store)
    finally:
        await store.stop()


# --------------------------------------------------------------------------- fake network


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class FakeNetwork:
    """Records every attempted post and decides which ones the backend accepts."""

    def __init__(self, *, fail_first: int = 0, fail_on: set[str] | None = None,
                 status: int = 200, error_body: str = "gateway down") -> None:
        self.fail_first = fail_first
        self.fail_on = fail_on or set()
        self.status = status
        self.error_body = error_body
        self.attempts: list[str] = []      # event_id of every post we were asked to make
        self.bodies: list[dict] = []       # bodies the backend actually accepted
        self.urls: list[str] = []
        self.calls = 0

    async def post(self, url, *, json):
        self.calls += 1
        self.urls.append(url)
        self.attempts.append(json.get("event_id"))
        if self.calls <= self.fail_first or json.get("event_id") in self.fail_on:
            raise ConnectionError("edge link is down")
        if not 200 <= self.status < 300:
            return _FakeResponse(self.status, self.error_body)
        self.bodies.append(dict(json))
        return _FakeResponse(self.status)

    @property
    def delivered_ids(self) -> list[str]:
        return [b["event_id"] for b in self.bodies]


class _FakeClient:
    def __init__(self, net: FakeNetwork) -> None:
        self._net = net

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json):
        return await self._net.post(url, json=json)


@pytest.fixture
def install_network(monkeypatch):
    def install(net: FakeNetwork) -> FakeNetwork:
        monkeypatch.setitem(
            sys.modules, "httpx",
            SimpleNamespace(AsyncClient=lambda timeout=None: _FakeClient(net)),
        )
        return net

    return install


async def drain(fwd: StoreAndForward, *, max_passes: int = 25) -> int:
    """Flush until the outbox is empty, as the background flusher would over time."""

    passes = 0
    while (await fwd.outbox.counts())["pending"] and passes < max_passes:
        await fwd.flush_once()
        passes += 1
    return passes


# --------------------------------------------------------------------------- outbox


async def test_enqueue_requires_an_event_id(outbox):
    with pytest.raises(ValueError, match="missing event_id"):
        await outbox.enqueue({"event_type": "ppe", "timestamp": 1.0})
    assert await outbox.counts() == {"pending": 0, "sent": 0}


async def test_enqueue_stores_the_full_event_payload(outbox):
    row = await outbox.enqueue(
        {"event_id": "ev-1", "event_type": "fall", "timestamp": 1_700_000_000.0,
         "payload": {"zone": "zone-a"}}
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_error"] is None
    assert row["timestamp"] == 1_700_000_000.0
    assert row["flushed_at"] is None
    pending = await outbox.pending()
    assert len(pending) == 1
    assert pending[0]["event"]["payload"] == {"zone": "zone-a"}
    assert pending[0]["event_type"] == "fall"


async def test_enqueuing_the_same_event_twice_collapses_to_one_row(outbox):
    first = await outbox.enqueue({"event_id": "ev-1", "event_type": "ppe", "timestamp": 5.0})
    second = await outbox.enqueue({"event_id": "ev-1", "event_type": "ppe", "timestamp": 5.0})
    assert await outbox.counts() == {"pending": 1, "sent": 0}
    # the original queue position is preserved, so replay order is not reshuffled by a retry
    assert second["enqueued_at"] == first["enqueued_at"]


async def test_a_sent_event_is_never_resurrected_to_pending(outbox):
    await outbox.enqueue({"event_id": "ev-1", "event_type": "ppe"})
    await outbox.mark_sent("ev-1")
    again = await outbox.enqueue({"event_id": "ev-1", "event_type": "ppe"})
    assert again["status"] == "sent"
    assert await outbox.counts() == {"pending": 0, "sent": 1}
    assert await outbox.pending() == []


async def test_pending_is_oldest_first(outbox):
    ids = [f"ev-{i}" for i in range(6)]
    for eid in ids:
        await outbox.enqueue({"event_id": eid, "event_type": "ppe"})
    assert [r["event_id"] for r in await outbox.pending()] == ids
    assert [r["event_id"] for r in await outbox.pending(limit=3)] == ids[:3]


async def test_mark_sent_and_mark_failed_update_the_row(outbox):
    await outbox.enqueue({"event_id": "ev-1", "event_type": "ppe"})
    await outbox.mark_failed("ev-1", "ConnectError: refused", 2)
    row = (await outbox.pending())[0]
    assert row["attempts"] == 2
    assert row["last_error"] == "ConnectError: refused"
    assert row["status"] == "pending"          # a failure never loses the event

    await outbox.mark_sent("ev-1")
    assert await outbox.counts() == {"pending": 0, "sent": 1}


async def test_mark_failed_truncates_a_huge_error(outbox):
    await outbox.enqueue({"event_id": "ev-1"})
    await outbox.mark_failed("ev-1", "x" * 5000, 1)
    assert len((await outbox.pending())[0]["last_error"]) == 500


async def test_purge_sent_respects_the_retention_window(outbox):
    for eid in ("old", "fresh"):
        await outbox.enqueue({"event_id": eid, "event_type": "ppe"})
        await outbox.mark_sent(eid)
    await outbox.enqueue({"event_id": "still-queued", "event_type": "ppe"})
    # age one acknowledged row past the window (no sleeping in tests)
    await outbox._table.patch("old", {"flushed_at": time.time() - 200_000})

    assert await outbox.purge_sent(older_than_s=86_400) == 1
    assert await outbox._table.get("old") is None
    assert await outbox.counts() == {"pending": 1, "sent": 1}

    # nothing else is due yet
    assert await outbox.purge_sent(older_than_s=86_400) == 0

    # a window that covers everything acknowledged still leaves pending rows alone
    assert await outbox.purge_sent(older_than_s=-1.0) == 1
    assert await outbox.counts() == {"pending": 1, "sent": 0}
    assert [r["event_id"] for r in await outbox.pending()] == ["still-queued"]


# --------------------------------------------------------------------------- forwarder


async def test_flush_without_central_api_is_a_reported_no_op(outbox):
    fwd = StoreAndForward(outbox)
    await fwd.submit({"event_id": "ev-1", "event_type": "ppe"})
    assert await fwd.flush_once() == {
        "sent": 0, "failed": 0, "reason": "no central_api configured",
    }
    # offline-only is not an error: the event is still safely queued
    assert await outbox.counts() == {"pending": 1, "sent": 0}
    assert fwd.online is None
    assert fwd.flushed_total == 0


async def test_flush_with_an_empty_queue_marks_us_online(outbox, install_network):
    net = install_network(FakeNetwork())
    fwd = StoreAndForward(outbox, central_api=CENTRAL)
    assert await fwd.flush_once() == {"sent": 0, "failed": 0}
    assert fwd.online is True
    assert net.calls == 0


async def test_outage_delivers_every_event_exactly_once_and_in_order(outbox, install_network):
    net = install_network(FakeNetwork(fail_first=3))
    fwd = StoreAndForward(outbox, central_api=CENTRAL)

    ids = [f"ev-{i}" for i in range(6)]
    for i, eid in enumerate(ids):
        await fwd.submit({"event_id": eid, "event_type": "ppe", "timestamp": 1000.0 + i, "seq": i})
    assert (await outbox.counts())["pending"] == 6
    assert net.calls == 0                       # submit never touches the network

    passes = await drain(fwd)

    # zero loss, zero duplication, original order
    assert net.delivered_ids == ids
    assert len(set(net.delivered_ids)) == len(ids)
    assert [b["seq"] for b in net.bodies] == list(range(6))
    # the head of the queue was retried and only the head was retried
    assert net.attempts == ["ev-0"] * 4 + ids[1:]
    assert await outbox.counts() == {"pending": 0, "sent": 6}
    assert fwd.flushed_total == 6
    assert fwd.online is True
    assert passes == 4
    assert net.urls[0] == f"{CENTRAL}/events"


async def test_flush_once_stops_at_the_first_failure(outbox, install_network):
    net = install_network(FakeNetwork(fail_on={"ev-1"}))
    fwd = StoreAndForward(outbox, central_api=CENTRAL)
    for eid in ("ev-0", "ev-1", "ev-2"):
        await fwd.submit({"event_id": eid, "event_type": "ppe"})

    assert await fwd.flush_once() == {"sent": 1, "failed": 1}
    # ev-2 is never attempted ahead of the stuck ev-1 — ordering beats throughput
    assert net.attempts == ["ev-0", "ev-1"]
    assert net.delivered_ids == ["ev-0"]
    assert fwd.online is False
    assert [r["event_id"] for r in await outbox.pending()] == ["ev-1", "ev-2"]
    stuck = (await outbox.pending())[0]
    assert stuck["attempts"] == 1
    assert "ConnectionError" in stuck["last_error"]


async def test_repeated_failures_accumulate_attempts_without_losing_the_event(
    outbox, install_network
):
    net = install_network(FakeNetwork(fail_on={"ev-0"}))
    fwd = StoreAndForward(outbox, central_api=CENTRAL)
    await fwd.submit({"event_id": "ev-0", "event_type": "ppe"})
    for _ in range(3):
        assert await fwd.flush_once() == {"sent": 0, "failed": 1}
    assert net.attempts == ["ev-0"] * 3
    assert (await outbox.pending())[0]["attempts"] == 3
    assert await outbox.counts() == {"pending": 1, "sent": 0}
    assert fwd.flushed_total == 0


async def test_a_non_2xx_response_is_a_failure_not_an_acknowledgement(outbox, install_network):
    install_network(FakeNetwork(status=503, error_body="upstream unavailable"))
    fwd = StoreAndForward(outbox, central_api=CENTRAL)
    await fwd.submit({"event_id": "ev-0", "event_type": "ppe"})

    assert await fwd.flush_once() == {"sent": 0, "failed": 1}
    row = (await outbox.pending())[0]
    assert row["last_error"] == "HTTP 503: upstream unavailable"
    assert await outbox.counts() == {"pending": 1, "sent": 0}


async def test_batch_size_caps_one_pass(outbox, install_network):
    net = install_network(FakeNetwork())
    fwd = StoreAndForward(outbox, central_api=CENTRAL, batch_size=2)
    for i in range(5):
        await fwd.submit({"event_id": f"ev-{i}", "event_type": "ppe"})
    assert await fwd.flush_once() == {"sent": 2, "failed": 0}
    assert net.delivered_ids == ["ev-0", "ev-1"]
    await drain(fwd)
    assert net.delivered_ids == [f"ev-{i}" for i in range(5)]


# --------------------------------------------------------------------------- reconciliation


async def test_replayed_event_keeps_its_original_timestamp(outbox, install_network):
    net = install_network(FakeNetwork())
    fwd = StoreAndForward(outbox, central_api=CENTRAL + "/")
    hazard_ts = 1_700_000_000.0
    await fwd.submit({"event_id": "ev-late", "event_type": "fall", "timestamp": hazard_ts,
                      "worker_id": "w-7"})
    # simulate a five-minute outage by ageing the queued row rather than sleeping
    aged = time.time() - 300.0
    await outbox._table.patch("ev-late", {"enqueued_at": aged})

    assert await fwd.flush_once() == {"sent": 1, "failed": 0}
    body = net.bodies[0]

    # history is not rewritten: the hazard timestamp is exactly what was recorded
    assert body["timestamp"] == hazard_ts
    assert body["worker_id"] == "w-7"
    # delivery metadata says it arrived late
    assert body["enqueued_at"] == pytest.approx(aged, abs=0.01)
    assert body["forwarded_at"] >= body["enqueued_at"]
    assert body["offline_delay_s"] == pytest.approx(300.0, abs=5.0)
    assert body["replayed"] is True
    assert net.urls == [f"{CENTRAL}/events"]      # trailing slash on central_api is normalised


async def test_a_live_event_is_not_flagged_as_replayed(outbox, install_network):
    net = install_network(FakeNetwork())
    fwd = StoreAndForward(outbox, central_api=CENTRAL)
    await fwd.submit({"event_id": "ev-live", "event_type": "ppe", "timestamp": 42.0})
    await fwd.flush_once()

    body = net.bodies[0]
    assert body["timestamp"] == 42.0
    assert body["replayed"] is False
    assert 0.0 <= body["offline_delay_s"] < 1.0
    assert set(body) >= {"enqueued_at", "forwarded_at", "offline_delay_s", "replayed"}


async def test_offline_delay_is_never_negative(outbox, install_network):
    net = install_network(FakeNetwork())
    fwd = StoreAndForward(outbox, central_api=CENTRAL)
    await fwd.submit({"event_id": "ev-skew", "event_type": "ppe"})
    await outbox._table.patch("ev-skew", {"enqueued_at": time.time() + 600.0})  # clock skew
    await fwd.flush_once()
    assert net.bodies[0]["offline_delay_s"] == 0.0


# --------------------------------------------------------------------------- lifecycle / status


async def test_status_reports_queue_depth(outbox, install_network):
    install_network(FakeNetwork())
    fwd = StoreAndForward(outbox, central_api=CENTRAL)
    await fwd.submit({"event_id": "ev-0", "event_type": "ppe"})
    assert await fwd.status() == {
        "central_api": CENTRAL, "online": None, "flushed_total": 0, "pending": 1, "sent": 0,
    }
    await fwd.flush_once()
    assert await fwd.status() == {
        "central_api": CENTRAL, "online": True, "flushed_total": 1, "pending": 0, "sent": 1,
    }


async def test_background_flusher_drains_after_an_outage_clears(outbox, install_network):
    net = install_network(FakeNetwork(fail_first=1))
    fwd = StoreAndForward(outbox, central_api=CENTRAL, flush_interval_s=0.01)
    await fwd.start()
    try:
        for i in range(3):
            await fwd.submit({"event_id": f"ev-{i}", "event_type": "ppe"})
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (await outbox.counts())["pending"]:
            await asyncio.sleep(0.01)
    finally:
        await fwd.stop()

    assert net.delivered_ids == ["ev-0", "ev-1", "ev-2"]
    assert await outbox.counts() == {"pending": 0, "sent": 3}
