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
from starlette.websockets import WebSocketDisconnect

from fieldpilot.backend.app import create_app
from fieldpilot.core.config import Config
from tests.conftest import MANAGER, WORKER1, WORKER2, login


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


def _alert_for(client, headers=None, **kw) -> dict:
    assert client.post("/events", json=_ppe_event(**kw)).status_code == 202
    for _ in range(40):
        time.sleep(0.1)
        alerts = client.get("/alerts", headers=headers).json()["alerts"]
        if alerts:
            return alerts[0]
    raise AssertionError("no alert was created")


# ------------------------------------------------------------------ zones


def test_default_zones_are_seeded_with_hazard_levels(client):
    _, manager_h = login(client, MANAGER)
    zones = client.get("/zones", headers=manager_h).json()["zones"]
    assert len(zones) == 4
    by_id = {z["zone_id"]: z for z in zones}
    assert by_id["zone-a"]["hazard_level"] == "high"
    assert by_id["zone-a"]["danger"] is True
    assert by_id["zone-d"]["hazard_level"] == "low"
    assert by_id["zone-d"]["danger"] is False


def test_zone_crud_round_trip(client):
    _, manager_h = login(client, MANAGER)
    created = client.post("/zones", json={"name": "Zone Z — Crane Pad", "hazard_level": "high"},
                          headers=manager_h)
    assert created.status_code == 201
    zid = created.json()["zone_id"]
    assert zid == "zone-z-crane-pad"          # slugified from the name
    assert created.json()["danger"] is True    # high hazard implies a danger zone by default

    assert client.get(f"/zones/{zid}", headers=manager_h).json()["name"] == "Zone Z — Crane Pad"

    updated = client.put(f"/zones/{zid}", json={"description": "tower crane base", "active": False},
                         headers=manager_h)
    assert updated.status_code == 200
    assert updated.json()["description"] == "tower crane base"
    assert updated.json()["active"] is False

    assert client.delete(f"/zones/{zid}", headers=manager_h).json()["deleted"] is True
    assert client.get(f"/zones/{zid}", headers=manager_h).status_code == 404
    assert client.delete(f"/zones/{zid}", headers=manager_h).status_code == 404


def test_zone_validation_and_duplicates_are_rejected(client):
    _, manager_h = login(client, MANAGER)
    assert client.post("/zones", json={"name": "  "}, headers=manager_h).status_code == 400
    assert client.post("/zones", json={"name": "X", "hazard_level": "extreme"},
                       headers=manager_h).status_code == 400
    assert client.post("/zones", json={"name": "Dupe"}, headers=manager_h).status_code == 201
    assert client.post("/zones", json={"name": "Dupe"}, headers=manager_h).status_code == 400
    assert client.put("/zones/zone-a", json={"hazard_level": "bogus"},
                      headers=manager_h).status_code == 400
    assert client.put("/zones/nonexistent", json={"description": "x"},
                      headers=manager_h).status_code == 404


def test_active_only_filter(client):
    _, manager_h = login(client, MANAGER)
    client.put("/zones/zone-d", json={"active": False}, headers=manager_h)
    active = client.get("/zones", params={"active_only": "true"},
                        headers=manager_h).json()["zones"]
    assert "zone-d" not in {z["zone_id"] for z in active}


# ------------------------------------------------------------------ feedback


def test_feedback_records_the_detector_class_and_bbox(client):
    _, manager_h = login(client, MANAGER)
    alert = _alert_for(client, headers=manager_h)
    r = client.post(f"/alerts/{alert['alert_id']}/feedback",
                    json={"decision": "approve", "reviewer": "jo", "notes": "correct"},
                    headers=manager_h)
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "approve"
    assert body["label"] == "NO-Hardhat"      # taken from payload["class"], not event_type
    assert body["bbox"] == [10, 20, 110, 220]
    assert body["reviewer"] == "jo"
    assert body["consumed_at"] is None

    stats = client.get("/feedback/stats", headers=manager_h).json()
    assert stats == {"approved": 1, "rejected": 0, "total": 1,
                     "unconsumed": 1, "approval_rate": 1.0}


def test_rejecting_an_alert_suppresses_it(client):
    _, manager_h = login(client, MANAGER)
    alert = _alert_for(client, headers=manager_h)
    assert alert["state"] in ("NEW", "ACTIVE")
    r = client.post(f"/alerts/{alert['alert_id']}/feedback", json={"decision": "reject"},
                    headers=manager_h)
    assert r.status_code == 200
    after = client.get(f"/alerts/{alert['alert_id']}", headers=manager_h).json()
    assert after["state"] == "SUPPRESSED", "a rejected detection is also a live false positive"


def test_feedback_errors(client):
    _, manager_h = login(client, MANAGER)
    alert = _alert_for(client, headers=manager_h)
    assert client.post(f"/alerts/{alert['alert_id']}/feedback",
                       json={"decision": "perhaps"}, headers=manager_h).status_code == 400
    assert client.post("/alerts/does-not-exist/feedback",
                       json={"decision": "approve"}, headers=manager_h).status_code == 404


def test_feedback_listing_filters(client):
    _, manager_h = login(client, MANAGER)
    alert = _alert_for(client, headers=manager_h)
    client.post(f"/alerts/{alert['alert_id']}/feedback", json={"decision": "approve"},
               headers=manager_h)
    assert len(client.get("/feedback", params={"decision": "approve"},
                         headers=manager_h).json()["feedback"]) == 1
    assert len(client.get("/feedback", params={"decision": "reject"},
                         headers=manager_h).json()["feedback"]) == 0
    assert len(client.get("/feedback", params={"event_type": "ppe"},
                         headers=manager_h).json()["feedback"]) == 1
    assert len(client.get("/feedback", params={"unconsumed_only": "true"},
                         headers=manager_h).json()["feedback"]) == 1


# ------------------------------------------------------------------ learning


def test_training_is_blocked_without_enough_feedback_and_says_why(client):
    _, manager_h = login(client, MANAGER)
    r = client.post("/learning/train", json={"epochs": 1}, headers=manager_h)
    assert r.status_code == 200
    run = r.json()
    assert run["status"] == "blocked"
    assert "at least 8" in run["message"]
    assert run["promoted"] is False
    assert run["map50_before"] is None

    runs = client.get("/learning/runs", headers=manager_h).json()["runs"]
    assert len(runs) == 1 and runs[0]["run_id"] == run["run_id"]
    assert client.get(f"/learning/runs/{run['run_id']}",
                      headers=manager_h).json()["status"] == "blocked"
    assert client.get("/learning/runs/nope", headers=manager_h).status_code == 404


def test_latest_learning_run_is_empty_before_any_completed_run(client):
    _, manager_h = login(client, MANAGER)
    assert client.get("/learning/latest", headers=manager_h).json() in (None, {})


# ------------------------------------------------------------------ RAG (degraded)


def test_blueprint_status_is_honest_when_qdrant_is_down(client):
    _, manager_h = login(client, MANAGER)
    body = client.get("/blueprints", headers=manager_h).json()
    assert body["available"] is False
    assert body["indexed_chunks"] == 0
    assert body["documents"] == []
    # Ollama is unreachable too, so the embedder must admit it is not semantic
    assert body["embeddings"]["semantic"] is False
    assert body["embeddings"]["backend"] == "lexical-fallback"


def test_ingest_refuses_rather_than_pretending(client):
    _, manager_h = login(client, MANAGER)
    assert client.post("/blueprints/ingest", json={"replace": False},
                       headers=manager_h).status_code == 503


def test_search_returns_empty_without_an_index(client):
    _, manager_h = login(client, MANAGER)
    r = client.post("/blueprints/search", json={"query": "rebar spacing", "zone": "zone-a"},
                    headers=manager_h)
    assert r.status_code == 200
    assert r.json()["chunks"] == []


# ------------------------------------------------------------------ RFI review queue


def _seed_rfi(client, zone="zone-a", headers=None) -> dict:
    """Drive a measurement deviation big enough to trip the seeded `rebar-deviation-rfi`."""

    client.post("/events", json={
        "camera_id": "cam-3", "zone": zone, "timestamp": time.time(),
        "event_type": "measurement", "confidence": 0.95, "severity": "medium",
        "payload": {"element": "rebar_spacing", "measured_mm": 47.5, "expected_mm": 20.0,
                    "deviation_mm": 27.5, "tolerance_mm": 5.0, "dedup_key": f"rebar-{zone}"},
    })
    for _ in range(60):
        time.sleep(0.1)
        rfis = client.get("/rfis", headers=headers).json()["rfis"]
        if rfis:
            return rfis[0]
    raise AssertionError("no RFI was drafted")


def test_rfi_is_filed_pending_review_and_flagged_ungrounded_without_specs(client):
    _, manager_h = login(client, MANAGER)
    rfi = _seed_rfi(client, headers=manager_h)
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
    _, manager_h = login(client, MANAGER)
    rfi = _seed_rfi(client, headers=manager_h)
    rid = rfi["rfi_id"]
    assert len(client.get("/rfis", params={"status": "pending_review"},
                         headers=manager_h).json()["rfis"]) == 1

    approved = client.post(f"/rfis/{rid}/approve", json={"reviewer": "sam", "notes": "valid"},
                           headers=manager_h)
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviewer"] == "sam"
    assert approved.json()["reviewed_at"] is not None
    assert len(client.get("/rfis", params={"status": "pending_review"},
                         headers=manager_h).json()["rfis"]) == 0

    rejected = client.post(f"/rfis/{rid}/reject", json={"reviewer": "sam"}, headers=manager_h)
    assert rejected.json()["status"] == "rejected"

    assert client.get(f"/rfis/{rid}", headers=manager_h).json()["status"] == "rejected"
    assert client.get("/rfis/missing", headers=manager_h).status_code == 404
    assert client.post("/rfis/missing/approve", json={}, headers=manager_h).status_code == 404


def test_inspection_completion(client):
    _, manager_h = login(client, MANAGER)
    client.post("/events", json={
        "camera_id": "cam-2", "zone": "zone-b", "timestamp": time.time(),
        "event_type": "crack", "confidence": 0.95, "severity": "high",
        "payload": {"defect": "Severerotation", "severity_score": 0.93, "dedup_key": "crk-1"},
    })
    insp = None
    for _ in range(40):
        time.sleep(0.1)
        items = client.get("/inspections", headers=manager_h).json()["inspections"]
        if items:
            insp = items[0]
            break
    assert insp is not None, "severe crack should request an inspection"
    assert insp["status"] == "requested"

    done = client.post(f"/inspections/{insp['inspection_id']}/complete",
                       json={"notes": "repaired"}, headers=manager_h)
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["notes"] == "repaired"
    assert done.json()["completed_at"] is not None
    assert client.post("/inspections/missing/complete", json={},
                       headers=manager_h).status_code == 404


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
    _, manager_h = login(client, MANAGER)
    manager_token = manager_h["Authorization"].removeprefix("Bearer ")
    _, w1_h = login(client, WORKER1)
    worker1_token = w1_h["Authorization"].removeprefix("Bearer ")
    _, w2_h = login(client, WORKER2)
    worker2_token = w2_h["Authorization"].removeprefix("Bearer ")

    # `kind` and `worker_id` are no longer client-declared (C3) — the socket derives both from
    # whose token this is, so a site manager's token is what makes `dash` a dashboard, and each
    # worker's own token is what makes their socket a device scoped to their own worker_id.
    with client.websocket_connect(f"/ws?token={manager_token}") as dash, \
         client.websocket_connect(f"/ws?token={worker1_token}&zone=zone-a") as dev_a, \
         client.websocket_connect(f"/ws?token={worker2_token}&zone=zone-b") as dev_b:
        for sock in (dash, dev_a, dev_b):
            assert sock.receive_json()["topic"] == "hello"

        stats = client.get("/broadcast/clients", headers=manager_h).json()["stats"]
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
        client.post("/zones", json={"name": "probe zone"}, headers=manager_h)
        sentinel = _recv(dev_b)
        assert sentinel["topic"] == "zone", (
            f"a zone-b device received {sentinel['topic']!r} before the sentinel — "
            "a zone-a advisory leaked across zones"
        )


def test_websocket_rejects_a_missing_or_invalid_token(client):
    """C3: no token, and a token that does not resolve, must both fail the handshake."""

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=not-a-real-token"):
            pass


def test_websocket_derives_identity_from_the_token_not_the_query_string(client):
    """C3: `?kind=dashboard&worker_id=w-2` must not override what the worker's own token says."""

    _, manager_h = login(client, MANAGER)
    _, w1_h = login(client, WORKER1)
    worker1_token = w1_h["Authorization"].removeprefix("Bearer ")

    with client.websocket_connect(
        f"/ws?token={worker1_token}&kind=dashboard&worker_id=w-2&zone=zone-a"
    ) as sock:
        hello = sock.receive_json()
        assert hello["data"]["kind"] == "device", "a worker's socket is always `device`"

        clients = client.get("/broadcast/clients", headers=manager_h).json()["clients"]
        mine = next(c for c in clients if c["client_id"] == hello["data"]["client_id"])
        assert mine["worker_id"] == "w-1", "worker_id must come from the token, not the query"
        assert mine["zone"] == "zone-a", "zone is still a legitimate client-supplied filter"
