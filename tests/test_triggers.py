"""Intelligent Trigger Engine: dedup window, merging, state machine, auto-resolve, suppress."""

from __future__ import annotations

import asyncio

from fieldpilot.events.bus import InMemoryEventBus
from fieldpilot.events.schema import Event, EventType, Severity
from fieldpilot.triggers.cache import InMemoryTriggerCache
from fieldpilot.triggers.engine import AlertState, TriggerEngine


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_engine(clock, bus=None, sink=None, **kw):
    defaults = dict(dedup_window_s=45.0, resolve_after_s=90.0, cache_ttl_s=3600)
    defaults.update(kw)
    return TriggerEngine(InMemoryTriggerCache(), bus, alert_sink=sink, clock=clock, **defaults)


def ppe_event(worker="w-1", item="helmet", ts=1000.0, severity=Severity.MEDIUM) -> Event:
    return Event(
        worker_id=worker, camera_id="cam-1", zone="zone-a",
        timestamp=ts, event_type=EventType.PPE, confidence=0.9, severity=severity,
        payload={"ppe_item": item, "dedup_key": item, "message": f"missing {item}"},
    )


# --------------------------------------------------------------------------- dedup


async def test_duplicate_within_45s_is_suppressed():
    clock = FakeClock()
    engine = make_engine(clock)

    r1 = await engine.process(ppe_event())
    assert r1.outcome == "created" and r1.notified
    assert r1.alert.state == AlertState.NEW

    clock.advance(10)  # within the 45 s window
    r2 = await engine.process(ppe_event(ts=clock.t))
    assert r2.outcome == "suppressed_duplicate"
    assert not r2.notified
    assert r2.alert.hit_count == 2
    assert r2.alert.alert_id == r1.alert.alert_id  # merged into ONE alert


async def test_repeat_after_window_confirms_active_not_new():
    clock = FakeClock()
    engine = make_engine(clock)

    r1 = await engine.process(ppe_event())
    clock.advance(50)  # past the 45 s window
    r2 = await engine.process(ppe_event(ts=clock.t))
    assert r2.outcome == "merged"
    assert r2.alert.state == AlertState.ACTIVE
    assert r2.alert.alert_id == r1.alert.alert_id
    assert r2.alert.hit_count == 2


async def test_500_per_minute_scenario_collapses_to_one_alert():
    """The spec's nightmare: a PPE model firing 500 detections/minute on one worker."""

    clock = FakeClock()
    engine = make_engine(clock)
    first = await engine.process(ppe_event())
    outcomes = []
    for _i in range(499):
        clock.advance(0.12)  # 500 events inside one minute
        outcomes.append((await engine.process(ppe_event(ts=clock.t))).outcome)
    assert set(outcomes) == {"suppressed_duplicate"}
    alert = await engine.get(first.alert.dedup_key)
    assert alert.hit_count == 500


async def test_different_workers_or_items_are_not_merged():
    clock = FakeClock()
    engine = make_engine(clock)
    r1 = await engine.process(ppe_event(worker="w-1", item="helmet"))
    r2 = await engine.process(ppe_event(worker="w-2", item="helmet"))
    r3 = await engine.process(ppe_event(worker="w-1", item="vest"))
    assert len({r1.alert.alert_id, r2.alert.alert_id, r3.alert.alert_id}) == 3


# --------------------------------------------------------------------------- auto-resolve


async def test_auto_resolve_when_issue_disappears():
    clock = FakeClock()
    bus = InMemoryEventBus()
    resolved: list[dict] = []

    async def on_resolved(topic, msg):
        resolved.append(msg)

    await bus.subscribe("alerts.resolved", on_resolved)
    await bus.start()
    engine = make_engine(clock, bus=bus, resolve_after_s=90)

    await engine.process(ppe_event())
    clock.advance(45)
    assert await engine.sweep_once() == []         # still fresh → stays active
    clock.advance(46)                               # 91 s since last detection
    out = await engine.sweep_once()
    assert len(out) == 1
    assert out[0].state == AlertState.RESOLVED
    await asyncio.sleep(0.05)
    assert len(resolved) == 1
    await bus.stop()


async def test_reactivation_after_resolve_renotifies():
    clock = FakeClock()
    engine = make_engine(clock, resolve_after_s=90)
    r1 = await engine.process(ppe_event())
    clock.advance(100)
    await engine.sweep_once()
    clock.advance(5)
    r2 = await engine.process(ppe_event(ts=clock.t))
    assert r2.outcome == "reactivated"
    assert r2.notified
    assert r2.alert.state == AlertState.ACTIVE
    assert r2.alert.alert_id == r1.alert.alert_id


# --------------------------------------------------------------------------- suppression


async def test_operator_suppress_silences_future_detections():
    clock = FakeClock()
    engine = make_engine(clock)
    r1 = await engine.process(ppe_event())
    await engine.suppress(r1.alert.dedup_key)

    clock.advance(60)
    r2 = await engine.process(ppe_event(ts=clock.t))
    assert r2.outcome == "suppressed"
    assert not r2.notified
    assert r2.alert.state == AlertState.SUPPRESSED

    await engine.unsuppress(r1.alert.dedup_key)
    clock.advance(60)
    r3 = await engine.process(ppe_event(ts=clock.t))
    assert r3.outcome == "merged"  # back to normal processing


async def test_severity_escalates_on_merge():
    clock = FakeClock()
    engine = make_engine(clock)
    await engine.process(ppe_event(severity=Severity.MEDIUM))
    clock.advance(10)
    r = await engine.process(ppe_event(severity=Severity.CRITICAL, ts=clock.t))
    assert r.alert.severity == "critical"


async def test_alert_sink_persists_every_transition():
    clock = FakeClock()
    saved: list[dict] = []

    async def sink(alert_dict):
        saved.append(alert_dict)

    engine = make_engine(clock, sink=sink)
    await engine.process(ppe_event())
    clock.advance(100)
    await engine.sweep_once()
    assert len(saved) >= 2
    assert saved[0]["state"] == "NEW"
    assert saved[-1]["state"] == "RESOLVED"
