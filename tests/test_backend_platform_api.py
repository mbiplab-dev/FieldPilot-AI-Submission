"""REST + WebSocket surface added for zones, feedback, learning, RAG and RFI review.

Hermetic: SQLite store, in-memory bus, no Qdrant/Ollama/Redis. The RAG endpoints are therefore
exercised in their *degraded* form, which is itself worth pinning — a missing vector store must
produce an honest "unavailable" answer rather than a 500.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

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
        # point RAG at a closed port so `start()` fails fast instead of hanging on a retry
        "reasoning": {"qdrant_url": "http://127.0.0.1:1", "ollama_host": "http://127.0.0.1:1",
                      "project_id": "riverside", "blueprints_dir": str(tmp_path / "bp")},
        "learning": {"val_set": str(tmp_path / "val"), "output_dir": str(tmp_path / "out"),
                     "base_weights": str(tmp_path / "nope.pt"), "min_samples": 8},
        "detection": {"ppe_model": str(tmp_path / "missing_ppe.pt")},
    })
    with TestClient(create_app(cfg)) as c:
        yield c


def _recv(sock, timeout: float = 10.0) -> dict:
    """Receive one WebSocket frame, failing instead of hanging.

    `TestClient`'s websocket `receive_json()` blocks indefinitely, so a routing regression that
    delivers nothing would hang the suite rather than report a failure. Reading on a worker
    thread turns that into a clean assertion error.
    """

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sock.receive_json)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            pytest.fail(f"no WebSocket frame arrived within {timeout}s")
        finally:
            pool.shutdown(wait=False)


def _recv_until(sock, topic: str, max_frames: int = 8) -> list[dict]:
    """Read frames until `topic` appears; returns everything read, that frame last."""

    seen: list[dict] = []
    for _ in range(max_frames):
        seen.append(_recv(sock))
        if seen[-1]["topic"] == topic:
            return seen
    pytest.fail(f"topic {topic!r} never arrived; saw {[f['topic'] for f in seen]}")


def _ppe_event(worker="w-1", item="helmet", zone="zone-a", cls="NO-Hardhat"):
    return {
        "worker_id": worker, "camera_id": "cam-1", "zone": zone,
        "timestamp": time.time(), "event_type": "ppe", "confidence": 0.9, "severity": "high",
        "payload": {"ppe_item": item, "dedup_key": item, "class": cls,
                    "bbox": [10, 20, 110, 220], "message": f"missing {item}"},
    }


def _alert_for(client, **kw) -> dict:
    assert client.post("/events", json=_ppe_event(**kw)).status_code == 202
    for _ in range(40):
        time.sleep(0.1)
        alerts = client.get("/alerts").json()["alerts"]
        if alerts:
            return alerts[0]
    raise AssertionError("no alert was created")


# ------------------------------------------------------------------ zones


def test_default_zones_are_seeded_with_hazard_levels(client):
    zones = client.get("/zones").json()["zones"]
    assert len(zones) == 4
    by_id = {z["zone_id"]: z for z in zones}
    assert by_id["zone-a"]["hazard_level"] == "high"
    assert by_id["zone-a"]["danger"] is True
    assert by_id["zone-d"]["hazard_level"] == "low"
    assert by_id["zone-d"]["danger"] is False


def test_zone_crud_round_trip(client):
    created = client.post("/zones", json={"name": "Zone Z — Crane Pad", "hazard_level": "high"})
    assert created.status_code == 201
    zid = created.json()["zone_id"]
    assert zid == "zone-z-crane-pad"          # slugified from the name
    assert created.json()["danger"] is True    # high hazard implies a danger zone by default

    assert client.get(f"/zones/{zid}").json()["name"] == "Zone Z — Crane Pad"

    updated = client.put(f"/zones/{zid}", json={"description": "tower crane base", "active": False})
    assert updated.status_code == 200
    assert updated.json()["description"] == "tower crane base"
    assert updated.json()["active"] is False

    assert client.delete(f"/zones/{zid}").json()["deleted"] is True
    assert client.get(f"/zones/{zid}").status_code == 404
    assert client.delete(f"/zones/{zid}").status_code == 404


def test_zone_validation_and_duplicates_are_rejected(client):
    assert client.post("/zones", json={"name": "  "}).status_code == 400
    assert client.post("/zones", json={"name": "X", "hazard_level": "extreme"}).status_code == 400
    assert client.post("/zones", json={"name": "Dupe"}).status_code == 201
    assert client.post("/zones", json={"name": "Dupe"}).status_code == 400
    assert client.put("/zones/zone-a", json={"hazard_level": "bogus"}).status_code == 400
    assert client.put("/zones/nonexistent", json={"description": "x"}).status_code == 404


def test_active_only_filter(client):
    client.put("/zones/zone-d", json={"active": False})
    active = client.get("/zones", params={"active_only": "true"}).json()["zones"]
    assert "zone-d" not in {z["zone_id"] for z in active}


# ------------------------------------------------------------------ feedback


def test_feedback_records_the_detector_class_and_bbox(client):
    alert = _alert_for(client)
    r = client.post(f"/alerts/{alert['alert_id']}/feedback",
                    json={"decision": "approve", "reviewer": "jo", "notes": "correct"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "approve"
    assert body["label"] == "NO-Hardhat"      # taken from payload["class"], not event_type
    assert body["bbox"] == [10, 20, 110, 220]
    assert body["reviewer"] == "jo"
    assert body["consumed_at"] is None

    stats = client.get("/feedback/stats").json()
    assert stats == {"approved": 1, "rejected": 0, "total": 1,
                     "unconsumed": 1, "approval_rate": 1.0}


def test_rejecting_an_alert_suppresses_it(client):
    alert = _alert_for(client)
    assert alert["state"] in ("NEW", "ACTIVE")
    r = client.post(f"/alerts/{alert['alert_id']}/feedback", json={"decision": "reject"})
    assert r.status_code == 200
    after = client.get(f"/alerts/{alert['alert_id']}").json()
    assert after["state"] == "SUPPRESSED", "a rejected detection is also a live false positive"


def test_feedback_errors(client):
    alert = _alert_for(client)
    assert client.post(f"/alerts/{alert['alert_id']}/feedback",
                       json={"decision": "perhaps"}).status_code == 400
    assert client.post("/alerts/does-not-exist/feedback",
                       json={"decision": "approve"}).status_code == 404


def test_feedback_listing_filters(client):
    alert = _alert_for(client)
    client.post(f"/alerts/{alert['alert_id']}/feedback", json={"decision": "approve"})
    assert len(client.get("/feedback", params={"decision": "approve"}).json()["feedback"]) == 1
    assert len(client.get("/feedback", params={"decision": "reject"}).json()["feedback"]) == 0
    assert len(client.get("/feedback", params={"event_type": "ppe"}).json()["feedback"]) == 1
    assert len(client.get("/feedback", params={"unconsumed_only": "true"}).json()["feedback"]) == 1


# ------------------------------------------------------------------ learning


def test_training_is_blocked_without_enough_feedback_and_says_why(client):
    r = client.post("/learning/train", json={"epochs": 1})
    assert r.status_code == 200
    run = r.json()
    assert run["status"] == "blocked"
    assert "at least 8" in run["message"]
    assert run["promoted"] is False
    assert run["map50_before"] is None

    runs = client.get("/learning/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["run_id"] == run["run_id"]
    assert client.get(f"/learning/runs/{run['run_id']}").json()["status"] == "blocked"
    assert client.get("/learning/runs/nope").status_code == 404


def test_latest_learning_run_is_empty_before_any_completed_run(client):
    assert client.get("/learning/latest").json() in (None, {})


# ------------------------------------------------------------------ RAG (degraded)


def test_blueprint_status_is_honest_when_qdrant_is_down(client):
    body = client.get("/blueprints").json()
    assert body["available"] is False
    assert body["indexed_chunks"] == 0
    assert body["documents"] == []
    # Ollama is unreachable too, so the embedder must admit it is not semantic
    assert body["embeddings"]["semantic"] is False
    assert body["embeddings"]["backend"] == "lexical-fallback"


def test_ingest_refuses_rather_than_pretending(client):
    assert client.post("/blueprints/ingest", json={"replace": False}).status_code == 503


def test_search_returns_empty_without_an_index(client):
    r = client.post("/blueprints/search", json={"query": "rebar spacing", "zone": "zone-a"})
    assert r.status_code == 200
    assert r.json()["chunks"] == []


# ------------------------------------------------------------------ RFI review queue


def _seed_rfi(client, zone="zone-a") -> dict:
    """Drive a measurement deviation big enough to trip the seeded `rebar-deviation-rfi`."""

    client.post("/events", json={
        "camera_id": "cam-3", "zone": zone, "timestamp": time.time(),
        "event_type": "measurement", "confidence": 0.95, "severity": "medium",
        "payload": {"element": "rebar_spacing", "measured_mm": 47.5, "expected_mm": 20.0,
                    "deviation_mm": 27.5, "tolerance_mm": 5.0, "dedup_key": f"rebar-{zone}"},
    })
    for _ in range(60):
        time.sleep(0.1)
        rfis = client.get("/rfis").json()["rfis"]
        if rfis:
            return rfis[0]
    raise AssertionError("no RFI was drafted")


def test_rfi_is_filed_pending_review_and_flagged_ungrounded_without_specs(client):
    rfi = _seed_rfi(client)
    assert rfi["status"] == "pending_review"
    assert rfi["zone"] == "zone-a"
    # no blueprint index is reachable, so the RFI must declare itself ungrounded rather than
    # inventing a clause reference
    assert rfi["payload"]["grounded"] is False
    assert rfi["citation"] is None
    assert "UNGROUNDED" in (rfi["body"] or "") or "none" in (rfi["body"] or "").lower()
    # the measured numbers are stated deterministically, not left to the LLM
    assert "27.5" in (rfi["body"] or "")


def test_rfi_approve_and_reject_transitions(client):
    rfi = _seed_rfi(client)
    rid = rfi["rfi_id"]
    assert len(client.get("/rfis", params={"status": "pending_review"}).json()["rfis"]) == 1

    approved = client.post(f"/rfis/{rid}/approve", json={"reviewer": "sam", "notes": "valid"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviewer"] == "sam"
    assert approved.json()["reviewed_at"] is not None
    assert len(client.get("/rfis", params={"status": "pending_review"}).json()["rfis"]) == 0

    rejected = client.post(f"/rfis/{rid}/reject", json={"reviewer": "sam"})
    assert rejected.json()["status"] == "rejected"

    assert client.get(f"/rfis/{rid}").json()["status"] == "rejected"
    assert client.get("/rfis/missing").status_code == 404
    assert client.post("/rfis/missing/approve", json={}).status_code == 404


def test_inspection_completion(client):
    client.post("/events", json={
        "camera_id": "cam-2", "zone": "zone-b", "timestamp": time.time(),
        "event_type": "crack", "confidence": 0.95, "severity": "high",
        "payload": {"defect": "Severerotation", "severity_score": 0.93, "dedup_key": "crk-1"},
    })
    insp = None
    for _ in range(40):
        time.sleep(0.1)
        items = client.get("/inspections").json()["inspections"]
        if items:
            insp = items[0]
            break
    assert insp is not None, "severe crack should request an inspection"
    assert insp["status"] == "requested"

    done = client.post(f"/inspections/{insp['inspection_id']}/complete", json={"notes": "repaired"})
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["notes"] == "repaired"
    assert done.json()["completed_at"] is not None
    assert client.post("/inspections/missing/complete", json={}).status_code == 404


# ------------------------------------------------------------------ health / websocket


def test_health_reports_every_subsystem(client):
    h = client.get("/health").json()
    assert h["status"] == "ok"
    assert h["zones"] == 4
    assert h["feedback"]["total"] == 0
    assert h["rag"]["available"] is False
    assert h["broadcast"]["connected"] == 0
    # the configured PPE weights do not exist, and health must say so plainly
    assert h["ppe"]["enabled"] is False
    assert "fetch-models" in h["ppe"]["reason"]


def test_websocket_delivers_alerts_to_dashboards_and_advisories_to_zone_devices(client):
    with client.websocket_connect("/ws?kind=dashboard") as dash, \
         client.websocket_connect("/ws?kind=device&zone=zone-a&worker_id=w-9") as dev_a, \
         client.websocket_connect("/ws?kind=device&zone=zone-b&worker_id=w-3") as dev_b:
        for sock in (dash, dev_a, dev_b):
            assert sock.receive_json()["topic"] == "hello"

        stats = client.get("/broadcast/clients").json()["stats"]
        assert stats["connected"] == 3
        assert stats["devices"] == 2 and stats["dashboards"] == 1

        client.post("/events", json=_ppe_event(worker="w-42"))

        dash_frames = _recv_until(dash, "alert")
        assert dash_frames[-1]["topic"] == "alert", "dashboard must receive the alert"

        # A device must get the advisory and nothing else, so assert on the FIRST frame rather
        # than searching a batch — that is what proves alerts/notifications were filtered out.
        first = _recv(dev_a)
        assert first["topic"] == "advisory", (
            f"a device's first frame must be the advisory, got {first['topic']!r} — "
            "alerts and dashboard notifications must not reach devices"
        )
        advisory = first["data"]
        assert advisory["zone"] == "zone-a"
        assert advisory["severity"] == "low", "advisories are downgraded secondary alerts"
        assert advisory["origin_worker_id"] == "w-42"

        dev_a.send_json({"type": "ping"})
        assert dev_a.receive_json()["topic"] == "pong"

        # zone-b must stay quiet. Proven by ordering: publish a zone-less sentinel that every
        # client receives, and require it to be the FIRST thing the zone-b device sees. If the
        # zone-a advisory had leaked, it would already be queued ahead of the sentinel.
        client.post("/zones", json={"name": "probe zone"})
        sentinel = _recv(dev_b)
        assert sentinel["topic"] == "zone", (
            f"a zone-b device received {sentinel['topic']!r} before the sentinel — "
            "a zone-a advisory leaked across zones"
        )
