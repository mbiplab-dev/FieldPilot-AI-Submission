"""Authenticated worker-assistant API behavior without Ollama or model weights."""

from __future__ import annotations

import io

from tests.conftest import MANAGER, WORKER1, login


def test_worker_can_route_a_spoken_measurement_command(client):
    _, headers = login(client, WORKER1)
    response = client.post(
        "/assistant/query",
        data={"text": "Hey FieldPilot, measure this opening"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "measure"
    assert body["action"] == {"type": "open_measurement"}


def test_worker_report_is_prepared_but_not_automatically_sent(client):
    _, headers = login(client, WORKER1)
    body = client.post(
        "/assistant/query", data={"text": "Report smoke by the stairs"}, headers=headers,
    ).json()
    assert body["requires_confirmation"] is True
    assert body["action"]["event_type"] == "fire"
    assert body["action"]["severity"] == "critical"
    assert client.get("/events", params={"worker_id": "w-1"}, headers=headers).json()["events"] == []


def test_worker_can_calculate_a_reference_measurement(client):
    _, headers = login(client, WORKER1)
    response = client.post(
        "/assistant/measure",
        json={
            "reference_points": [[10, 10], [210, 10]],
            "measurement_points": [[20, 50], [220, 50]],
            "reference_mm": 100,
            "spec_mm": 90,
            "tolerance_mm": 5,
            "image_size": [640, 480],
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["measured_mm"] == 100
    assert response.json()["within_tolerance"] is False


def test_assistant_rejects_non_image_attachment(client):
    _, headers = login(client, WORKER1)
    response = client.post(
        "/assistant/query",
        data={"text": "identify this"},
        files={"image": ("payload.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 400


def test_manager_cannot_impersonate_the_worker_assistant(client):
    _, headers = login(client, MANAGER)
    assert client.post(
        "/assistant/query", data={"text": "report fire"}, headers=headers,
    ).status_code == 403


def test_assistant_requires_authentication(client):
    assert client.get("/assistant/status").status_code == 401
    assert client.post("/assistant/query", data={"text": "hello"}).status_code == 401
