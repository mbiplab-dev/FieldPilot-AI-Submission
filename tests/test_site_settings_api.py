"""Operator-editable site settings and the alert-board summary.

These are the `hrm` branch's dashboard controls (per-item PPE toggles, confidence/pose tuning,
detector selection, alert stats) rebuilt on this platform's storage and event bus.
"""

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
        "reasoning": {"qdrant_url": "http://127.0.0.1:1", "ollama_host": "http://127.0.0.1:1",
                      "blueprints_dir": str(tmp_path / "bp")},
        "learning": {"val_set": str(tmp_path / "val"), "output_dir": str(tmp_path / "out")},
        "detection": {"ppe_model": str(tmp_path / "missing.pt"), "conf_min": 0.35,
                      "models_dir": str(tmp_path / "models")},
        "safety": {"tracked_items": {"helmet": True, "vest": True, "gloves": False,
                                     "boots": False, "goggles": False}},
    })
    with TestClient(create_app(cfg)) as c:
        yield c


def _ppe_event(worker="w-1", item="helmet", cls="NO-Hardhat", dedup=None, severity="high"):
    return {
        "worker_id": worker, "camera_id": "cam-1", "zone": "zone-a", "timestamp": time.time(),
        "event_type": "ppe", "confidence": 0.9, "severity": severity,
        "payload": {"ppe_item": item, "class": cls, "dedup_key": dedup or item},
    }


def _wait_for_alerts(client, count=1, budget=6.0):
    deadline = time.time() + budget
    while time.time() < deadline:
        alerts = client.get("/alerts").json()["alerts"]
        if len(alerts) >= count:
            return alerts
        time.sleep(0.1)
    return client.get("/alerts").json()["alerts"]


# ------------------------------------------------------------------ site config


def test_config_exposes_boot_defaults_from_yaml(client):
    body = client.get("/config").json()
    assert body["tracked_items"] == {
        "helmet": True, "vest": True, "gloves": False, "boots": False, "goggles": False,
    }
    assert body["confidence_threshold"] == 0.35
    assert body["pose_enabled"] is True
    assert body["available_items"] == ["helmet", "vest", "gloves", "boots", "goggles"]
    # the configured weights are absent in this fixture, and config must say so
    assert body["ppe_weights"]["enabled"] is False


def test_tracked_item_toggle_persists_and_is_reflected(client):
    r = client.post("/config/tracked-items", json={"item_name": "gloves", "enabled": True})
    assert r.status_code == 200
    assert r.json()["tracked_items"]["gloves"] is True
    assert client.get("/config").json()["tracked_items"]["gloves"] is True

    client.post("/config/tracked-items", json={"item_name": "helmet", "enabled": False})
    items = client.get("/config").json()["tracked_items"]
    assert items["helmet"] is False and items["gloves"] is True, "toggles are independent"


def test_unknown_tracked_item_is_rejected(client):
    r = client.post("/config/tracked-items", json={"item_name": "jetpack", "enabled": True})
    assert r.status_code == 400
    assert "jetpack" in r.json()["detail"]


def test_monitoring_settings_validate_and_persist(client):
    r = client.post("/config/monitoring", json={"confidence_threshold": 0.55,
                                                "pose_enabled": False})
    assert r.status_code == 200
    assert r.json()["confidence_threshold"] == 0.55
    assert r.json()["pose_enabled"] is False
    cfg = client.get("/config").json()
    assert cfg["confidence_threshold"] == 0.55 and cfg["pose_enabled"] is False


@pytest.mark.parametrize("bad", [0.05, 0.99, -1.0])
def test_confidence_threshold_outside_the_usable_band_is_rejected(client, bad):
    assert client.post("/config/monitoring", json={"confidence_threshold": bad}).status_code == 400


def test_partial_monitoring_update_leaves_the_other_field_alone(client):
    client.post("/config/monitoring", json={"confidence_threshold": 0.6, "pose_enabled": False})
    client.post("/config/monitoring", json={"confidence_threshold": 0.7})
    cfg = client.get("/config").json()
    assert cfg["confidence_threshold"] == 0.7
    assert cfg["pose_enabled"] is False, "pose_enabled must not be reset by a partial update"


# ------------------------------------------------------------------ model registry


def test_models_endpoint_lists_the_registry(client):
    body = client.get("/models").json()
    keys = {m["key"] for m in body["models"]}
    # the four pinned public PPE checkpoints ported from hrm must all be offered
    assert {"ppe_construction_n", "ppe_helmet_vest_n", "ppe_safetyvision_s", "ppe_vyra_m"} <= keys
    for m in body["models"]:
        assert {"key", "label", "capability", "downloaded"} <= set(m)
        assert m["capability"] in ("ppe", "person")


def test_selecting_an_unknown_model_is_rejected(client):
    assert client.post("/models/select",
                       json={"model_key": "not-a-model"}).status_code == 400


def test_model_choice_is_recorded_without_downloading(client):
    r = client.post("/models/select", json={"model_key": "ppe_construction_n",
                                            "download": False})
    assert r.status_code == 200
    assert r.json()["model_key"] == "ppe_construction_n"
    assert r.json()["weights"] is None, "download=False must not fetch anything"
    assert client.get("/models").json()["selected"] == "ppe_construction_n"


def test_select_with_download_awaits_the_registry_and_returns_a_real_path(client, monkeypatch,
                                                                         tmp_path):
    """`ensure_weights` is a coroutine; the endpoint must await it, not hand back a coroutine.

    Wrapping an async function in `run_in_threadpool` returns an un-awaited coroutine, which
    stringifies into a bogus "weights" path instead of fetching anything. Exercised here because
    the `download=False` path cannot catch it.
    """

    fake = tmp_path / "models" / "ppe_construction_n.pt"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"weights")
    calls: list[tuple[str, str]] = []

    async def _ensure(key, models_dir, *, force=False):
        calls.append((key, str(models_dir)))
        return fake

    monkeypatch.setattr("fieldpilot.models_registry.ensure_weights", _ensure)

    r = client.post("/models/select", json={"model_key": "ppe_construction_n", "download": True})
    assert r.status_code == 200, r.text
    assert calls == [("ppe_construction_n", str(tmp_path / "models"))]
    assert r.json()["weights"] == str(fake)
    assert "coroutine" not in r.json()["weights"]


def test_select_surfaces_a_download_failure_instead_of_a_500(client, monkeypatch):
    from fieldpilot.models_registry import ModelRegistryError

    async def _boom(key, models_dir, *, force=False):
        raise ModelRegistryError("checksum verification failed")

    monkeypatch.setattr("fieldpilot.models_registry.ensure_weights", _boom)
    r = client.post("/models/select", json={"model_key": "ppe_vyra_m", "download": True})
    assert r.status_code == 400
    assert "checksum" in r.json()["detail"]


# ------------------------------------------------------------------ alert stats


def test_alert_stats_summarises_the_board(client):
    client.post("/events", json=_ppe_event(worker="w-1", dedup="helmet-1"))
    client.post("/events", json=_ppe_event(worker="w-2", item="vest",
                                           cls="NO-Safety Vest", dedup="vest-1"))
    alerts = _wait_for_alerts(client, 2)
    assert len(alerts) >= 2

    stats = client.get("/alerts/stats").json()
    assert stats["total"] >= 2
    assert stats["today"] >= 2
    assert stats["outstanding"] >= 2
    assert stats["by_item"], "per-item breakdown must not be empty"
    assert set(stats["by_item"]) <= {"helmet", "vest", "NO-Hardhat", "NO-Safety Vest", "ppe"}
    assert stats["by_severity"].get("high", 0) >= 2


def test_stats_route_is_not_shadowed_by_the_alert_id_route(client):
    """`/alerts/stats` must resolve to the summary, not be read as an alert id."""

    r = client.get("/alerts/stats")
    assert r.status_code == 200
    assert "outstanding" in r.json()


def test_acknowledging_an_alert_resolves_it_and_updates_stats(client):
    client.post("/events", json=_ppe_event(dedup="ack-1"))
    alerts = _wait_for_alerts(client, 1)
    alert_id = alerts[0]["alert_id"]
    assert alerts[0]["state"] in ("NEW", "ACTIVE")

    before = client.get("/alerts/stats").json()["outstanding"]
    r = client.post(f"/alerts/{alert_id}/acknowledge")
    assert r.status_code == 200

    assert client.get(f"/alerts/{alert_id}").json()["state"] == "RESOLVED"
    after = client.get("/alerts/stats").json()
    assert after["outstanding"] == before - 1
    assert after["resolved"] >= 1
