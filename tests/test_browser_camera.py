"""Browser-webcam ingest: `/ws/video` frame round-trip, robustness, and bus integration.

Hermetic. No camera, no model weights, no network: the vision engine is replaced with a stub that
returns a fixed COCO-17 person, the PPE/inspection detectors are pointed at absent weights (they
degrade to "disabled", which is their documented behaviour), and the server-side video source is a
file path that does not exist so the `/dev/video0` pipeline exits immediately and leaves the
WebSocket path as the only producer.

What is worth pinning here is not "a JSON blob comes back" but that a browser frame is *not* a
second, parallel universe: the same detector stack runs, and any hazard it raises reaches the same
event bridge that carries camera-sourced hazards onto the platform bus.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from fieldpilot.core.config import Config
from fieldpilot.core.types import (
    NUM_KEYPOINTS,
    Frame,
    FrameResult,
    HazardEvent,
    HazardType,
    PersonDetection,
    Severity,
)
from fieldpilot.display.server import LatestFrame, create_app, decode_jpeg, normalise_class

EXPECTED_KEYS = {"frame", "detections", "poses", "counts", "inference_ms", "hazards", "dropped"}


# --------------------------------------------------------------------------- stubs


class _StubEngine:
    """Stands in for `VisionEngine`: one tracked person with a full COCO-17 skeleton."""

    def __init__(self, cfg) -> None:
        self.calls = 0

    def infer(self, frame: Frame) -> FrameResult:
        self.calls += 1
        keypoints = np.zeros((NUM_KEYPOINTS, 3), dtype=np.float32)
        for i in range(NUM_KEYPOINTS):
            keypoints[i] = (20.0 + i, 30.0 + i, 0.9)
        person = PersonDetection(
            track_id=7, bbox=(10.0, 20.0, 60.0, 120.0), conf=0.81, keypoints=keypoints
        )
        return FrameResult(frame=frame, persons=[person], infer_ms=3.5)


class _SpyBridge:
    """Records every hazard handed to the bridge, then converts it exactly as the real one does."""

    seen: list[tuple[HazardEvent, object]] = []

    def __init__(self, bus, *, camera_id: str = "cam-edge-0", zone: str | None = None) -> None:
        self.bus = bus
        self.camera_id = camera_id
        self.zone = zone

    async def emit(self, hazard: HazardEvent):
        from fieldpilot.events.bridge import hazard_to_event

        event = hazard_to_event(hazard, camera_id=self.camera_id, zone=self.zone)
        type(self).seen.append((hazard, event))
        return event


def _one_hazard_per_frame(self, result: FrameResult) -> list[HazardEvent]:
    """Patched `FallDetector.update` — a deterministic hazard so the bus path is exercised."""

    if not result.persons:
        return []
    person = result.persons[0]
    return [
        HazardEvent(
            hazard_type=HazardType.FALL,
            severity=Severity.HIGH,
            message=f"Worker {person.track_id} may have fallen.",
            frame_index=result.frame.index,
            ts_monotonic=result.frame.ts_monotonic,
            track_id=person.track_id,
            bbox=person.bbox,
        )
    ]


# --------------------------------------------------------------------------- fixtures


def _cfg(tmp_path) -> Config:
    return Config({
        # a file source that cannot be opened: the server-camera pipeline stops at once, so this
        # test never depends on hardware and never competes with the WebSocket path.
        "video": {"source": "file", "file_path": str(tmp_path / "no-such-video.mp4")},
        "detection": {"ppe_model": str(tmp_path / "absent_ppe.pt"), "keypoint_conf_min": 0.3},
        "inspection": {"enabled": False, "model": None},
        "storage": {"sqlite_path": str(tmp_path / "edge.db")},
        "logging": {"json_file": str(tmp_path / "events.jsonl"), "level": "WARNING"},
        "events": {"bus_backend": "memory", "camera_id": "cam-test", "zone": "zone-a"},
    })


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    """Edge app whose detectors are stubbed — `create_app` is otherwise untouched."""

    monkeypatch.setattr("fieldpilot.core.pipeline.VisionEngine", _StubEngine)
    with TestClient(create_app(_cfg(tmp_path), with_bus=False)) as client:
        yield client


def _jpeg(width: int = 96, height: int = 64) -> bytes:
    """A real, decodable JPEG built in-process."""

    image = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.rectangle(image, (10, 10), (60, 50), (40, 180, 90), -1)
    ok, buf = cv2.imencode(".jpg", image)
    assert ok, "cv2 could not encode the test frame"
    return buf.tobytes()


def _recv(sock, timeout: float = 20.0) -> dict:
    """Receive one WebSocket frame, failing instead of hanging.

    `TestClient`'s `receive_json()` blocks forever, so a regression that answers nothing would hang
    CI rather than report a failure. Reading on a worker thread turns that into an assertion.
    """

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sock.receive_json)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            pytest.fail(f"no WebSocket frame arrived within {timeout}s")
        finally:
            pool.shutdown(wait=False)


# --------------------------------------------------------------------------- round trip


def test_a_browser_jpeg_round_trips_to_detections(edge):
    with edge.websocket_connect("/ws/video") as sock:
        sock.send_bytes(_jpeg())
        body = _recv(sock)

    assert EXPECTED_KEYS <= set(body), f"missing keys: {EXPECTED_KEYS - set(body)}"
    assert body["frame"] == {"index": 0, "width": 96, "height": 64}

    person = next(d for d in body["detections"] if d["category"] == "person")
    assert person["class"] == "person"
    assert person["track_id"] == 7
    assert person["confidence"] == pytest.approx(0.81, abs=1e-3)
    assert person["box"] == [10.0, 20.0, 60.0, 120.0]
    assert person["is_violation"] is False

    assert len(body["poses"]) == 1
    assert len(body["poses"][0]["keypoints"]) == NUM_KEYPOINTS
    assert all(len(k) == 3 for k in body["poses"][0]["keypoints"])
    assert body["poses"][0]["track_id"] == 7

    assert body["counts"] == {"people": 1, "ppe_items": 0, "violations": 0, "poses": 1}
    assert isinstance(body["inference_ms"], (int, float)) and body["inference_ms"] >= 0
    assert body["hazards"] == []
    assert body["dropped"] == 0


def test_the_same_socket_serves_many_frames_with_rising_frame_indices(edge):
    with edge.websocket_connect("/ws/video") as sock:
        sock.send_bytes(_jpeg())
        first = _recv(sock)
        sock.send_bytes(_jpeg(128, 96))
        second = _recv(sock)

    assert first["frame"]["index"] == 0
    assert second["frame"]["index"] == 1
    # the frame geometry is read from the decoded image, not assumed
    assert second["frame"] == {"index": 1, "width": 128, "height": 96}


# --------------------------------------------------------------------------- robustness


def test_an_undecodable_payload_errors_and_leaves_the_socket_usable(edge):
    with edge.websocket_connect("/ws/video") as sock:
        sock.send_bytes(b"this is definitely not a JPEG")
        error = _recv(sock)
        assert "error" in error, f"expected an error frame, got {sorted(error)}"
        assert "decoded" in error["error"]
        assert "detections" not in error

        # the connection must survive a bad frame — the next good one is processed normally
        sock.send_bytes(_jpeg())
        good = _recv(sock)

    assert EXPECTED_KEYS <= set(good)
    assert good["counts"]["people"] == 1


def test_an_empty_payload_is_reported_rather_than_crashing_the_socket(edge):
    with edge.websocket_connect("/ws/video") as sock:
        sock.send_bytes(b"")
        assert "error" in _recv(sock)
        sock.send_bytes(_jpeg())
        assert "detections" in _recv(sock)


def test_a_burst_of_frames_never_stalls_the_socket(edge):
    """Back-pressure: bursts are answered (some frames coalesced), never queued forever."""

    with edge.websocket_connect("/ws/video") as sock:
        for _ in range(8):
            sock.send_bytes(_jpeg())
        body = _recv(sock)
        assert isinstance(body["dropped"], int) and body["dropped"] >= 0
        # still healthy afterwards
        sock.send_bytes(_jpeg())
        assert "counts" in _recv(sock)


# --------------------------------------------------------------------------- bus integration


def test_browser_hazards_reach_the_same_event_bridge_as_camera_hazards(tmp_path, monkeypatch):
    monkeypatch.setattr("fieldpilot.core.pipeline.VisionEngine", _StubEngine)
    monkeypatch.setattr("fieldpilot.safety.fall.FallDetector.update", _one_hazard_per_frame)
    monkeypatch.setattr("fieldpilot.events.bridge.PipelineEventBridge", _SpyBridge)
    _SpyBridge.seen = []

    with TestClient(create_app(_cfg(tmp_path), with_bus=True)) as client:
        with client.websocket_connect("/ws/video") as sock:
            sock.send_bytes(_jpeg())
            body = _recv(sock)

    assert len(body["hazards"]) == 1
    hazard = body["hazards"][0]
    assert hazard["type"] == "fall"
    assert hazard["severity"] == "high"
    assert hazard["track_id"] == 7
    assert hazard["bbox"] == [10.0, 20.0, 60.0, 120.0]
    # the person carrying the hazard is colour-coded as a violation for the overlay
    person = next(d for d in body["detections"] if d["category"] == "person")
    assert person["is_violation"] is True

    assert len(_SpyBridge.seen) == 1, "a browser-sourced hazard must reach the platform bridge"
    raised, event = _SpyBridge.seen[0]
    assert raised.id == hazard["id"]
    assert event.camera_id == "cam-test" and event.zone == "zone-a"
    assert event.event_type.value == "fall"
    # provenance survives onto the bus so a reviewer can tell which ingest path fired
    assert event.payload["ingest"] == "browser"


def test_hazards_are_dispatched_locally_when_no_bus_is_configured(tmp_path, monkeypatch):
    """Without `--bus` the edge must still act on browser hazards, via the local dispatcher."""

    monkeypatch.setattr("fieldpilot.core.pipeline.VisionEngine", _StubEngine)
    monkeypatch.setattr("fieldpilot.safety.fall.FallDetector.update", _one_hazard_per_frame)
    dispatched: list[HazardEvent] = []

    def _record(self, event):
        from fieldpilot.alerts.dispatcher import AlertRecord

        dispatched.append(event)
        return AlertRecord(event=event, admitted=True, latency_ms=1.0)

    monkeypatch.setattr("fieldpilot.alerts.dispatcher.AlertDispatcher.dispatch", _record)

    with TestClient(create_app(_cfg(tmp_path), with_bus=False)) as client:
        with client.websocket_connect("/ws/video") as sock:
            sock.send_bytes(_jpeg())
            body = _recv(sock)

    assert len(body["hazards"]) == 1
    assert [e.id for e in dispatched] == [body["hazards"][0]["id"]]


# --------------------------------------------------------------------------- fallback page


def test_camera_page_serves_a_self_contained_capture_page(edge):
    response = edge.get("/camera")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "<!doctype html>" in page.lower()
    for needle in ("getUserMedia", "enumerateDevices", "/ws/video", "isSecureContext"):
        assert needle in page, f"the fallback page must handle {needle}"
    # zero build step: nothing may be fetched from a CDN
    assert "http://" not in page and "https://" not in page


# --------------------------------------------------------------------------- unit level


def test_latest_frame_keeps_the_newest_and_counts_drops():
    """Pins the drop-oldest back-pressure policy independently of the socket."""

    async def scenario() -> None:
        slot = LatestFrame()
        slot.put(b"stale")
        slot.put(b"fresher")
        slot.put(b"newest")
        assert await slot.get() == b"newest"
        assert slot.dropped == 2, "superseded frames must be dropped, not queued"

        slot.put(b"after")
        slot.close()
        assert await slot.get() == b"after", "a frame already accepted is still delivered"
        assert await slot.get() is None, "None signals the producer is gone"

    asyncio.run(scenario())


def test_latest_frame_get_waits_for_a_producer():
    async def scenario() -> None:
        slot = LatestFrame()
        waiter = asyncio.ensure_future(slot.get())
        await asyncio.sleep(0)
        assert not waiter.done(), "get() must wait rather than return None on an empty slot"
        slot.put(b"frame")
        assert await asyncio.wait_for(waiter, timeout=2.0) == b"frame"

    asyncio.run(scenario())


def test_decode_jpeg_rejects_non_images_and_accepts_real_ones():
    assert decode_jpeg(b"") is None
    assert decode_jpeg(b"\x00\x01\x02not-an-image") is None
    image = decode_jpeg(_jpeg(48, 32))
    assert image is not None and image.shape == (32, 48, 3)


def test_class_names_are_normalised_for_the_wire():
    assert normalise_class("NO-Hardhat") == "no_hardhat"
    assert normalise_class("Safety Vest") == "safety_vest"
    assert normalise_class("  Severerotation ") == "severerotation"
    assert normalise_class("machinery/vehicle") == "machinery_vehicle"
