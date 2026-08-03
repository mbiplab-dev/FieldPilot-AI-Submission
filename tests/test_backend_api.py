"""REST API surface of the backend service (events, alerts, rules, workers)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from fieldpilot.backend.app import create_app
from fieldpilot.core.config import Config


@pytest.fixture()
def client(tmp_path):
    cfg = Config({
        "events": {
            "backend": "sqlite",
            "database_url": str(tmp_path / "platform.db"),
            "events_db_url": str(tmp_path / "events.db"),
            "bus_backend": "memory",
        },
        "triggers": {"dedup_window_s": 45, "resolve_after_s": 90, "sweep_interval_s": 1},
        "notifications": {"dedup_window_s": 300},
    })
    with TestClient(create_app(cfg)) as c:
        yield c


def _ppe_payload(worker="w-1", item="helmet", zone="zone-a"):
    return {
        "worker_id": worker, "camera_id": "cam-1", "zone": zone,
        "timestamp": time.time(), "event_type": "ppe", "confidence": 0.9,
        "severity": "medium",
        "payload": {"ppe_item": item, "dedup_key": item, "message": f"missing {item}"},
    }


def test_health_and_seeded_rules(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    rules = client.get("/rules").json()["rules"]
    assert len(rules) == 3  # the three spec exemplars seeded on first boot
    assert {r["name"] for r in rules} == {
        "no-helmet-in-danger-zone", "severe-crack-immediate-inspection", "rebar-deviation-rfi"}


def test_event_ingest_creates_single_deduped_alert(client):
    for _ in range(10):  # storm of duplicates
        r = client.post("/events", json=_ppe_payload())
        assert r.status_code == 202
    time.sleep(0.2)  # bus dispatch is async

    events = client.get("/events", params={"event_type": "ppe"}).json()["events"]
    assert len(events) == 10  # raw audit trail keeps everything

    alerts = client.get("/alerts", params={"event_type": "ppe"}).json()["alerts"]
    assert len(alerts) == 1  # one deduplicated alert
    assert alerts[0]["hit_count"] == 10
    assert alerts[0]["state"] in ("NEW", "ACTIVE")


def test_alert_resolve_and_suppress_flow(client):
    client.post("/events", json=_ppe_payload())
    time.sleep(0.2)
    alert = client.get("/alerts").json()["alerts"][0]
    aid = alert["alert_id"]

    r = client.post(f"/alerts/{aid}/suppress")
    assert r.json()["alert"]["state"] == "SUPPRESSED"
    r = client.post(f"/alerts/{aid}/unsuppress")
    assert r.json()["alert"]["state"] == "ACTIVE"
    r = client.post(f"/alerts/{aid}/resolve")
    assert r.json()["alert"]["state"] == "RESOLVED"

    assert client.post("/alerts/does-not-exist/resolve").status_code == 404


def test_alert_filters(client):
    client.post("/events", json=_ppe_payload(worker="w-1", zone="zone-a"))
    client.post("/events", json=_ppe_payload(worker="w-2", zone="zone-b"))
    time.sleep(0.2)
    by_worker = client.get("/alerts", params={"worker_id": "w-1"}).json()["alerts"]
    by_zone = client.get("/alerts", params={"zone": "zone-b"}).json()["alerts"]
    assert len(by_worker) == 1 and by_worker[0]["worker_id"] == "w-1"
    assert len(by_zone) == 1 and by_zone[0]["zone"] == "zone-b"


def test_rules_crud(client):
    rule = {
        "name": "test-rule", "priority": 50, "event_types": ["fall"],
        "conditions": [{"field": "event.severity", "op": "eq", "value": "high"}],
        "action": {"type": "notify", "message": "fall!"}, "cooldown_s": 60,
    }
    created = client.post("/rules", json=rule)
    assert created.status_code == 201
    rid = created.json()["rule_id"]

    got = client.get(f"/rules/{rid}")
    assert got.json()["name"] == "test-rule"

    updated = client.put(f"/rules/{rid}", json={**rule, "enabled": False})
    assert updated.json()["enabled"] is False

    assert client.delete(f"/rules/{rid}").json()["deleted"] == rid
    assert client.get(f"/rules/{rid}").status_code == 404


def test_worker_timeline(client):
    client.post("/events", json=_ppe_payload(worker="w-5"))
    time.sleep(0.2)
    tl = client.get("/workers/w-5/timeline").json()
    assert tl["worker_id"] == "w-5"
    assert tl["live_status"] == "flagged"
    assert 0 <= tl["safety_score"] < 100
    assert len(tl["active_alerts"]) == 1
    assert len(tl["recent_events"]) == 1

    workers = client.get("/workers").json()["workers"]
    assert any(w["worker_id"] == "w-5" for w in workers)


def test_notifications_endpoint(client):
    client.post("/events", json=_ppe_payload())
    time.sleep(0.2)
    notes = client.get("/notifications").json()["notifications"]
    assert len(notes) >= 1
    assert notes[0]["channel"] == "dashboard"


def test_inspection_control_endpoint(client):
    assert client.get("/control/inspection").json()["enabled"] is False
    r = client.post("/control/inspection", json={"enabled": True})
    assert r.json()["enabled"] is True
    assert client.get("/control/inspection").json()["enabled"] is True
    client.post("/control/inspection", json={"enabled": False})
    assert client.get("/control/inspection").json()["enabled"] is False
