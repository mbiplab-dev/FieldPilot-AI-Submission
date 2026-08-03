"""Broadcast hub: zone routing, audience scoping, self-exclusion and dead-socket eviction."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from fieldpilot.broadcast.hub import ADVISORY_SEVERITY, BroadcastHub, Client
from fieldpilot.events.bus import InMemoryEventBus


class FakeSocket:
    """Records the frames a client would have received."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send_json(self, frame: dict) -> None:
        if self.fail:
            raise RuntimeError("websocket already closed")
        self.sent.append(frame)

    @property
    def topics(self) -> list[str]:
        return [f["topic"] for f in self.sent]


async def until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    """Poll until `predicate` holds — the in-memory bus dispatches on a background task."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.fixture
async def hub():
    bus = InMemoryEventBus()
    await bus.start()
    h = BroadcastHub(bus)
    await h.start()
    try:
        yield h
    finally:
        await bus.stop()


def make_alert(**over):
    base = {
        "alert_id": "al-1",
        "event_type": "fall_detected",
        "zone": "zone-a",
        "worker_id": "w-9",
        "severity": "critical",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- Client.wants


def test_device_is_strictly_zone_scoped():
    dev = Client(ws=None, client_id="c1", kind="device", zone="zone-a")
    assert dev.listens_to_all_zones is False
    assert dev.wants(zone="zone-a", audience="device", exclude=None) is True
    assert dev.wants(zone="zone-b", audience="device", exclude=None) is False
    assert dev.wants(zone=None, audience="device", exclude=None) is True   # site-wide message


def test_wants_filters_on_audience():
    dev = Client(ws=None, client_id="c1", kind="device", zone="zone-a")
    dash = Client(ws=None, client_id="c2", kind="dashboard", zone=None)
    assert dev.wants(zone="zone-a", audience="dashboard", exclude=None) is False
    assert dev.wants(zone="zone-a", audience="all", exclude=None) is True
    assert dash.wants(zone="zone-a", audience="device", exclude=None) is False
    assert dash.wants(zone="zone-a", audience="all", exclude=None) is True
    assert dash.wants(zone="zone-a", audience="dashboard", exclude=None) is True


def test_wants_excludes_the_originating_client():
    dev = Client(ws=None, client_id="c1", kind="device", zone="zone-a")
    assert dev.wants(zone="zone-a", audience="device", exclude="c1") is False
    assert dev.wants(zone="zone-a", audience="device", exclude="c2") is True
    # exclusion wins even for a message the client would otherwise want site-wide
    assert dev.wants(zone=None, audience="all", exclude="c1") is False


def test_unpinned_client_listens_to_every_zone():
    for zone in (None, "", "all"):
        c = Client(ws=None, client_id="c", kind="dashboard", zone=zone)
        assert c.listens_to_all_zones is True
        assert c.wants(zone="zone-q", audience="all", exclude=None) is True


def test_pinned_dashboard_only_sees_its_zone():
    pinned = Client(ws=None, client_id="c", kind="dashboard", zone="zone-a")
    assert pinned.listens_to_all_zones is False
    assert pinned.wants(zone="zone-a", audience="all", exclude=None) is True
    assert pinned.wants(zone="zone-b", audience="all", exclude=None) is False


def test_describe_exposes_the_routing_fields():
    c = Client(ws=None, client_id="c1", kind="device", zone="zone-a", worker_id="w-3")
    d = c.describe()
    assert d["client_id"] == "c1"
    assert d["kind"] == "device"
    assert d["zone"] == "zone-a"
    assert d["worker_id"] == "w-3"
    assert isinstance(d["connected_at"], float)


# --------------------------------------------------------------------------- connections


async def test_connect_defaults_an_unknown_kind_to_dashboard(hub):
    client = await hub.connect(FakeSocket(), kind="robot")
    assert client.kind == "dashboard"
    assert len(client.client_id) == 12


async def test_stats_counts_clients_by_kind_and_zone(hub):
    await hub.connect(FakeSocket(), kind="device", zone="zone-a")
    await hub.connect(FakeSocket(), kind="device", zone="zone-b")
    dash = await hub.connect(FakeSocket(), kind="dashboard")

    assert hub.stats() == {
        "connected": 3, "devices": 2, "dashboards": 1,
        "by_zone": {"zone-a": 1, "zone-b": 1, "all": 1},
        "delivered": 0, "dropped": 0,
    }
    assert {c["client_id"] for c in hub.clients()} >= {dash.client_id}

    await hub.disconnect(dash)
    stats = hub.stats()
    assert stats["connected"] == 2 and stats["dashboards"] == 0
    assert stats["by_zone"] == {"zone-a": 1, "zone-b": 1}


async def test_stats_delivered_tracks_successful_sends(hub):
    sockets = [FakeSocket() for _ in range(3)]
    for ws in sockets:
        await hub.connect(ws, kind="device", zone="zone-a")
    await hub.publish("site", {"n": 1})
    assert await until(lambda: hub.stats()["delivered"] == 3)
    assert all(len(ws.sent) == 1 for ws in sockets)
    assert hub.stats()["dropped"] == 0


# --------------------------------------------------------------------------- routing


async def test_advisory_reaches_only_devices_in_the_alerts_zone(hub):
    ws_a, ws_b = FakeSocket(), FakeSocket()
    await hub.connect(ws_a, kind="device", zone="zone-a", worker_id="w-1")
    await hub.connect(ws_b, kind="device", zone="zone-b", worker_id="w-2")

    await hub.advise_zone(make_alert())

    assert await until(lambda: len(ws_a.sent) == 1)
    assert ws_b.sent == []

    frame = ws_a.sent[0]
    assert frame["topic"] == "advisory"
    assert frame["zone"] == "zone-a"
    assert isinstance(frame["ts"], float)
    data = frame["data"]
    assert data["alert_id"] == "al-1"
    assert data["event_type"] == "fall_detected"
    assert data["zone"] == "zone-a"
    assert data["origin_worker_id"] == "w-9"
    assert data["severity"] == ADVISORY_SEVERITY == "low"
    assert data["original_severity"] == "critical"
    assert data["message"] == (
        "Advisory: fall detected involving worker w-9 in your zone. Stay alert."
    )


async def test_advisory_excludes_the_originating_client(hub):
    ws_origin, ws_peer = FakeSocket(), FakeSocket()
    origin = await hub.connect(ws_origin, kind="device", zone="zone-a", worker_id="w-9")
    await hub.connect(ws_peer, kind="device", zone="zone-a", worker_id="w-1")

    await hub.advise_zone(make_alert(), exclude_client=origin.client_id)

    assert await until(lambda: len(ws_peer.sent) == 1)
    assert ws_origin.sent == []
    assert hub.stats()["delivered"] == 1


async def test_advisory_does_not_reach_dashboards(hub):
    ws_dev, ws_dash = FakeSocket(), FakeSocket()
    await hub.connect(ws_dev, kind="device", zone="zone-a")
    await hub.connect(ws_dash, kind="dashboard")

    await hub.advise_zone(make_alert())

    assert await until(lambda: len(ws_dev.sent) == 1)
    assert ws_dash.sent == []


async def test_advisory_without_a_zone_sends_nothing_at_all(hub):
    ws = FakeSocket()
    await hub.connect(ws, kind="device", zone="zone-a")

    await hub.advise_zone(make_alert(zone=None))
    await hub.advise_zone(make_alert(zone=""))
    # a sentinel proves the bus drained past the skipped advisories
    await hub.publish("ping", {"n": 1}, zone="zone-a", audience="device")

    assert await until(lambda: len(ws.sent) == 1)
    assert ws.topics == ["ping"]
    assert hub.stats()["delivered"] == 1


async def test_unpinned_dashboard_sees_every_zone_but_a_pinned_one_does_not(hub):
    ws_all, ws_pinned = FakeSocket(), FakeSocket()
    await hub.connect(ws_all, kind="dashboard")
    await hub.connect(ws_pinned, kind="dashboard", zone="zone-a")

    await hub.publish("alert", {"alert_id": "x1"}, zone="zone-q")
    assert await until(lambda: len(ws_all.sent) == 1)
    assert ws_pinned.sent == []
    assert ws_all.sent[0]["zone"] == "zone-q"
    assert ws_all.sent[0]["data"] == {"alert_id": "x1"}

    await hub.publish("alert", {"alert_id": "x2"}, zone="zone-a")
    assert await until(lambda: len(ws_pinned.sent) == 1)
    assert await until(lambda: len(ws_all.sent) == 2)
    assert ws_pinned.sent[0]["data"] == {"alert_id": "x2"}


async def test_zoneless_publish_reaches_everyone(hub):
    ws_a, ws_b, ws_dash = FakeSocket(), FakeSocket(), FakeSocket()
    await hub.connect(ws_a, kind="device", zone="zone-a")
    await hub.connect(ws_b, kind="device", zone="zone-b")
    await hub.connect(ws_dash, kind="dashboard", zone="zone-c")

    await hub.publish("evacuate", {"reason": "fire"})

    assert await until(lambda: all(len(ws.sent) == 1 for ws in (ws_a, ws_b, ws_dash)))
    assert ws_b.sent[0]["data"] == {"reason": "fire"}
    assert ws_b.sent[0]["zone"] is None


async def test_disconnected_client_stops_receiving(hub):
    ws = FakeSocket()
    client = await hub.connect(ws, kind="device", zone="zone-a")
    await hub.disconnect(client)

    await hub.advise_zone(make_alert())
    await asyncio.sleep(0.05)
    assert ws.sent == []
    assert hub.stats() == {
        "connected": 0, "devices": 0, "dashboards": 0, "by_zone": {},
        "delivered": 0, "dropped": 0,
    }


# --------------------------------------------------------------------------- dead sockets


async def test_a_failing_socket_is_dropped_and_counted(hub):
    good, bad = FakeSocket(), FakeSocket(fail=True)
    live = await hub.connect(good, kind="device", zone="zone-a")
    dead = await hub.connect(bad, kind="device", zone="zone-a")

    await hub.publish("advisory", {"m": 1}, zone="zone-a", audience="device")

    assert await until(lambda: hub.dropped == 1)
    assert len(good.sent) == 1                      # one bad peer does not block the others
    assert hub.delivered == 1
    ids = {c["client_id"] for c in hub.clients()}
    assert live.client_id in ids
    assert dead.client_id not in ids
    assert hub.stats()["connected"] == 1


async def test_hub_start_is_idempotent(hub):
    ws = FakeSocket()
    await hub.connect(ws, kind="device", zone="zone-a")
    await hub.start()          # a second subscribe would double-deliver every frame

    await hub.advise_zone(make_alert())
    assert await until(lambda: len(ws.sent) == 1)
    await asyncio.sleep(0.05)
    assert len(ws.sent) == 1
