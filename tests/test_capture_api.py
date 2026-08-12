import base64

import cv2
import numpy as np

from tests.conftest import MANAGER, WORKER1, login


def _jpeg() -> bytes:
    ok, payload = cv2.imencode(".jpg", np.full((60, 80, 3), 160, dtype=np.uint8))
    assert ok
    return payload.tobytes()


def test_capture_workflow_is_manager_only_and_serves_authenticated_images(client) -> None:
    _, manager_h = login(client, MANAGER)
    _, worker_h = login(client, WORKER1)
    assert client.get("/dataset/sessions", headers=worker_h).status_code == 403

    created = client.post(
        "/dataset/sessions", headers=manager_h, json={"name": "Phone run A", "split": "train"}
    )
    assert created.status_code == 200, created.text
    session = created.json()

    captured = client.post(
        f"/dataset/sessions/{session['session_id']}/frames",
        headers=manager_h,
        json={
            "jpeg_base64": base64.b64encode(_jpeg()).decode("ascii"),
            "detections": [
                {"class": "hardhat", "confidence": 0.91, "box": [8, 6, 40, 30]},
                {"class": "defect", "confidence": 0.99, "box": [0, 0, 10, 10]},
            ],
            "source_worker": "w-1",
            "zone": "zone-a",
            "captured_at": 123.0,
        },
    )
    assert captured.status_code == 200, captured.text
    frame = captured.json()
    assert "image_path" not in frame
    assert frame["boxes"][0]["label"] == "Hardhat"

    image = client.get(frame["image_url"], headers=manager_h)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert client.get(frame["image_url"], headers=worker_h).status_code == 403

    reviewed = client.put(
        f"/dataset/frames/{frame['frame_id']}",
        headers=manager_h,
        json={"boxes": frame["boxes"], "review_status": "reviewed"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review_status"] == "reviewed"


def test_capture_rejects_invalid_images_and_boxes(client) -> None:
    _, manager_h = login(client, MANAGER)
    session = client.post(
        "/dataset/sessions", headers=manager_h, json={"name": "Bad input", "split": "val"}
    ).json()

    bad_image = client.post(
        f"/dataset/sessions/{session['session_id']}/frames",
        headers=manager_h,
        json={"jpeg_base64": "not-base64", "source_worker": "w-1"},
    )
    assert bad_image.status_code == 400

    captured = client.post(
        f"/dataset/sessions/{session['session_id']}/frames",
        headers=manager_h,
        json={
            "jpeg_base64": base64.b64encode(_jpeg()).decode("ascii"),
            "source_worker": "w-1",
        },
    ).json()
    bad_box = client.put(
        f"/dataset/frames/{captured['frame_id']}",
        headers=manager_h,
        json={"boxes": [{"class_id": 99, "xyxy": [0, 0, 1, 1]}], "review_status": "reviewed"},
    )
    assert bad_box.status_code == 400
