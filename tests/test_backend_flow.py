"""End-to-end: Model → Event → Bus → Trigger → Rules → Notification/RFI/Inspection.

Verifies the platform invariant: a model only publishes an event; every observable
side-effect (stored event, deduplicated alert, RFI, inspection request, notification)
is produced by the downstream engine chain.
"""

from __future__ import annotations

import asyncio

from fieldpilot.backend.service import Orchestrator
from fieldpilot.backend.store import SQLitePlatformStore
from fieldpilot.events.bus import InMemoryEventBus, publish_event
from fieldpilot.events.schema import Event, EventType, Severity
from fieldpilot.events.store import SQLiteEventRepository
from fieldpilot.notifications.service import NotificationService
from fieldpilot.rules.engine import RuleEngine, default_rules
from fieldpilot.triggers.cache import InMemoryTriggerCache
from fieldpilot.triggers.engine import TriggerEngine


async def _stack(tmp_path):
    bus = InMemoryEventBus()
    cache = InMemoryTriggerCache()
    events = SQLiteEventRepository(str(tmp_path / "events.db"))
    store = SQLitePlatformStore(str(tmp_path / "platform.db"))
    triggers = TriggerEngine(cache, bus, dedup_window_s=45, resolve_after_s=90,
                             alert_sink=lambda a: store.upsert_alert(a))
    rules = RuleEngine(default_rules())
    notifications = NotificationService(store, cache, bus, dedup_window_s=300)
    orch = Orchestrator(bus=bus, events=events, store=store, triggers=triggers,
                        rules=rules, notifications=notifications)
    await bus.start()
    await events.start()
    await store.start()
    for r in default_rules():
        await store.put_rule(r.to_dict())
    await orch.start()
    return bus, events, store, triggers, rules, notifications


async def test_full_chain_ppe_in_danger_zone(tmp_path):
    bus, events, store, triggers, rules, notifications = await _stack(tmp_path)
    try:
        # 1. proximity event puts the worker in a danger zone
        await publish_event(bus, Event(
            worker_id="w-1", camera_id="cam-1", zone="zone-a",
            event_type=EventType.PROXIMITY, severity=Severity.HIGH,
            payload={"dedup_key": "proximity", "message": "near excavator"},
        ))
        await asyncio.sleep(0.1)

        # 2. PPE model fires a helmet violation for the same worker
        await publish_event(bus, Event(
            worker_id="w-1", camera_id="cam-1", zone="zone-a",
            event_type=EventType.PPE, severity=Severity.MEDIUM, confidence=0.92,
            payload={"ppe_item": "helmet", "dedup_key": "helmet", "message": "no helmet"},
        ))
        await asyncio.sleep(0.15)

        # every event persisted
        assert (await events.count_by_type()) == {"proximity": 1, "ppe": 1}

        # two deduplicated alerts tracked + persisted
        alerts = await store.list_alerts()
        assert len(alerts) == 2
        assert {a["event_type"] for a in alerts} == {"proximity", "ppe"}

        # rule "no-helmet-in-danger-zone" fired → critical notification sent to dashboard
        notes = await store.list_notifications()
        assert len(notes) >= 1
        assert any("CRITICAL" in (n["subject"] or "").upper() or "helmet" in (n["body"] or "")
                   for n in notes)
    finally:
        await bus.stop()
        await events.stop()
        await store.stop()


async def test_crack_severity_requests_inspection(tmp_path):
    bus, events, store, *_ = await _stack(tmp_path)
    try:
        await publish_event(bus, Event(
            worker_id=None, camera_id="cam-2", zone="zone-b",
            event_type=EventType.CRACK, severity=Severity.HIGH, confidence=0.95,
            payload={"severity_score": 0.91, "dedup_key": "crack-1"},
        ))
        await asyncio.sleep(0.15)
        inspections = await store.list_inspections()
        assert len(inspections) == 1
        assert inspections[0]["priority"] == "immediate"
        assert inspections[0]["zone"] == "zone-b"
    finally:
        await bus.stop()
        await events.stop()
        await store.stop()


async def test_rebar_deviation_generates_rfi(tmp_path):
    bus, events, store, *_ = await _stack(tmp_path)
    try:
        await publish_event(bus, Event(
            worker_id=None, camera_id="cam-3", zone="zone-c",
            event_type=EventType.MEASUREMENT, severity=Severity.MEDIUM, confidence=0.9,
            payload={"element": "rebar_spacing", "deviation_mm": 27.5,
                     "dedup_key": "rebar_spacing"},
        ))
        await asyncio.sleep(0.15)
        rfis = await store.list_rfis()
        assert len(rfis) == 1
        assert "27.5" in rfis[0]["summary"]
    finally:
        await bus.stop()
        await events.stop()
        await store.stop()


async def test_duplicate_storm_produces_single_alert(tmp_path):
    bus, events, store, triggers, *_ = await _stack(tmp_path)
    try:
        for _ in range(50):
            await publish_event(bus, Event(
                worker_id="w-9", camera_id="cam-1", zone="zone-a",
                event_type=EventType.PPE, severity=Severity.MEDIUM,
                payload={"ppe_item": "vest", "dedup_key": "vest"},
            ))
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.3)
        # raw events all stored (audit trail), but exactly ONE alert
        assert (await events.count_by_type())["ppe"] == 50
        alerts = await store.list_alerts(event_type="ppe")
        assert len(alerts) == 1
        assert alerts[0]["hit_count"] == 50
    finally:
        await bus.stop()
        await events.stop()
        await store.stop()


def test_bridge_converts_hazard_to_canonical_event():
    from fieldpilot.core.types import HazardEvent, HazardType
    from fieldpilot.core.types import Severity as EdgeSeverity
    from fieldpilot.events.bridge import hazard_to_event

    hz = HazardEvent(
        hazard_type=HazardType.PPE_MISSING, severity=EdgeSeverity.MEDIUM,
        message="Worker 3 is missing a hard hat.", frame_index=10, ts_monotonic=5.0,
        track_id=3, bbox=(1.0, 2.0, 3.0, 4.0), meta={"ppe": "helmet", "class": "NO-Hardhat"},
    )
    ev = hazard_to_event(hz, camera_id="cam-edge-0", zone="zone-a")
    assert ev.event_type == EventType.PPE
    assert ev.worker_id == "w-3"
    assert ev.zone == "zone-a"
    assert ev.payload["dedup_key"] == "helmet"
    assert ev.dedup_key() == "ppe:w-3:zone-a:helmet"
    assert ev.payload["message"] == "Worker 3 is missing a hard hat."


def test_bridge_maps_crack_hazard():
    from fieldpilot.core.types import HazardEvent, HazardType
    from fieldpilot.core.types import Severity as EdgeSeverity
    from fieldpilot.events.bridge import hazard_to_event
    from fieldpilot.events.schema import EventType

    hz = HazardEvent(
        hazard_type=HazardType.CRACK, severity=EdgeSeverity.HIGH,
        message="Severerotation detected.", frame_index=5, ts_monotonic=2.0,
        bbox=(10.0, 20.0, 30.0, 40.0),
        meta={"defect": "Severerotation", "severity_score": 0.92,
              "confidence": 0.95, "dedup_key": "severerotation:0:0"},
    )
    ev = hazard_to_event(hz, camera_id="cam-edge-0", zone="zone-b")
    assert ev.event_type == EventType.CRACK
    assert ev.worker_id is None  # cracks are infrastructure, not workers
    assert ev.payload["severity_score"] == 0.92
    assert ev.payload["defect"] == "Severerotation"
    assert ev.dedup_key() == "crack:-:zone-b:severerotation:0:0"
