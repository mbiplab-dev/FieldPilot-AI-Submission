"""Per-worker phone camera feeds: the registry, and the identified `/ws/video` relay path.

The phone is only a capture device — every detector runs on the server, exactly as it does for the
browser-camera page. What identification adds is provenance (which phone saw a hazard) and a live
view the site manager can watch.
"""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from fieldpilot.display.feeds import FeedRegistry, WorkerFeed
from fieldpilot.display.server import FrameOrigin, annotate, create_app, encode_jpeg

from .test_browser_camera import _cfg, _jpeg, _recv, _StubEngine

# --------------------------------------------------------------------------- registry


def test_a_feed_reports_itself_live_only_while_frames_keep_arriving():
    reg = FeedRegistry(stale_after_s=0.2)
    reg.open("w-1", zone="zone-a", display_name="Ravi")
    reg.publish("w-1", b"jpeg-bytes", width=640, height=480)

    live = reg.get("w-1")
    assert live["live"] is True
    assert live["zone"] == "zone-a" and live["display_name"] == "Ravi"
    assert live["frames"] == 1 and live["width"] == 640

    time.sleep(0.25)
    # The phone went quiet — backgrounded, out of signal, battery saver. Say so.
    assert reg.get("w-1")["live"] is False


def test_publishing_to_a_closed_feed_is_ignored_rather_than_resurrecting_it():
    reg = FeedRegistry()
    reg.open("w-1")
    reg.close("w-1")
    reg.publish("w-1", b"late-frame", width=10, height=10)

    assert reg.get("w-1") is None
    assert reg.frame("w-1") is None


def test_reconnecting_keeps_the_running_counters_but_takes_the_new_zone():
    reg = FeedRegistry()
    reg.open("w-1", zone="zone-a")
    reg.publish("w-1", b"a", width=10, height=10, hazards=2)

    # The worker walked into a different zone and the phone reconnected.
    reg.open("w-1", zone="zone-b")
    reg.publish("w-1", b"b", width=10, height=10, hazards=1)

    feed = reg.get("w-1")
    assert feed["zone"] == "zone-b"
    assert feed["frames"] == 2
    assert feed["hazards"] == 3


def test_only_the_newest_frame_is_kept():
    """Latest-frame-wins: a stale frame is worthless for safety."""

    reg = FeedRegistry()
    reg.open("w-1")
    for payload in (b"one", b"two", b"three"):
        reg.publish("w-1", payload, width=10, height=10)
    assert reg.frame("w-1") == b"three"


def test_feeds_are_listed_most_recently_active_first():
    reg = FeedRegistry()
    for wid in ("w-1", "w-2", "w-3"):
        reg.open(wid)
        reg.publish(wid, b"x", width=10, height=10)
        time.sleep(0.01)

    assert [f["worker_id"] for f in reg.list()] == ["w-3", "w-2", "w-1"]
    assert reg.stats()["streaming"] == 3
    assert set(reg.stats()["workers"]) == {"w-1", "w-2", "w-3"}


def test_a_feed_nothing_has_written_to_is_eventually_evicted():
    """A phone that vanished mid-shift must not linger in the dashboard forever."""

    reg = FeedRegistry()
    reg.open("w-gone")
    reg.publish("w-gone", b"x", width=10, height=10)
    reg.open("w-here")
    reg.publish("w-here", b"x", width=10, height=10)

    assert reg.evict_stale(older_than_s=-1) != []          # everything is "old" against -1s
    assert reg.list() == []

    reg.open("w-here")
    reg.publish("w-here", b"x", width=10, height=10)
    assert reg.evict_stale(older_than_s=300) == []          # fresh feeds survive
    assert len(reg.list()) == 1


def test_describe_never_leaks_the_frame_bytes():
    """The summary is JSON; pixels go over the MJPEG route, not inside a status payload."""

    feed = WorkerFeed(worker_id="w-1")
    feed.jpeg = b"\xff\xd8lots-of-pixels"
    assert "jpeg" not in feed.describe()
    assert b"pixels" not in repr(feed.describe()).encode()


def test_an_unknown_worker_has_no_feed_and_no_frame():
    reg = FeedRegistry()
    assert reg.get("nobody") is None
    assert reg.frame("nobody") is None
    assert reg.stats()["streaming"] == 0


# --------------------------------------------------------------------------- origin + annotate


def test_frame_origin_names_the_device_not_the_person_in_frame():
    origin = FrameOrigin(worker_id="w-9", zone="zone-a")
    # A rear camera mostly sees colleagues, so the camera id records whose *phone* it is.
    assert origin.camera_id == "phone-w-9"
    assert origin.ingest == "phone"


def test_annotate_draws_without_touching_the_original_frame():
    """The pristine frame is what alert snapshots keep — boxes are only for the relayed view."""

    image = np.zeros((60, 80, 3), dtype=np.uint8)
    before = image.copy()
    out = annotate(image, [{"box": [5, 5, 40, 40], "class": "person", "is_violation": True}])

    assert np.array_equal(image, before), "annotate must not mutate its input"
    assert not np.array_equal(out, before), "annotate drew nothing"


@pytest.mark.parametrize("detections", [
    [],
    [{"box": [1, 2, 3]}],                        # too few coordinates
    [{"class": "person"}],                       # no box at all
    [{"box": ["a", "b", "c", "d"]}],             # unparseable coordinates
])
def test_annotate_survives_malformed_detections(detections):
    image = np.zeros((40, 40, 3), dtype=np.uint8)
    try:
        out = annotate(image, detections)
    except (ValueError, TypeError):
        pytest.fail("a malformed detection must be skipped, not raise into the socket loop")
    assert out.shape == image.shape


def test_encode_jpeg_produces_a_decodable_jpeg():
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    cv2.rectangle(image, (4, 4), (28, 28), (30, 200, 90), -1)
    payload = encode_jpeg(image)

    assert payload and payload[:2] == b"\xff\xd8"
    assert cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR) is not None


# --------------------------------------------------------------------------- the live socket


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    monkeypatch.setattr("fieldpilot.core.pipeline.VisionEngine", _StubEngine)
    with TestClient(create_app(_cfg(tmp_path), with_bus=False)) as client:
        yield client


def test_an_anonymous_browser_stream_creates_no_worker_feed(edge):
    """The browser-camera page must keep working exactly as before, and stay anonymous."""

    with edge.websocket_connect("/ws/video") as sock:
        sock.send_bytes(_jpeg())
        body = _recv(sock)

    assert body["frame"]["index"] == 0
    assert edge.get("/workers/live").json()["feeds"] == []


def test_an_identified_phone_stream_becomes_a_watchable_feed(edge):
    with edge.websocket_connect("/ws/video?worker_id=w-1&zone=zone-a&name=Ravi") as sock:
        sock.send_bytes(_jpeg())
        body = _recv(sock)
        assert body["frame"]["width"] == 96

        listing = edge.get("/workers/live").json()
        feed = next(f for f in listing["feeds"] if f["worker_id"] == "w-1")
        assert feed["live"] is True
        assert feed["zone"] == "zone-a"
        assert feed["display_name"] == "Ravi"
        assert feed["width"] == 96 and feed["height"] == 64
        assert listing["stats"]["streaming"] == 1

        # The manager can pull the annotated frame the server produced.
        with edge.stream("GET", "/workers/w-1/stream") as relay:
            assert relay.status_code == 200
            assert "multipart/x-mixed-replace" in relay.headers["content-type"]
            chunk = next(relay.iter_bytes())
            assert b"--frame" in chunk and b"image/jpeg" in chunk


def test_the_feed_is_dropped_when_the_phone_disconnects(edge):
    with edge.websocket_connect("/ws/video?worker_id=w-2") as sock:
        sock.send_bytes(_jpeg())
        _recv(sock)
        assert any(f["worker_id"] == "w-2" for f in edge.get("/workers/live").json()["feeds"])

    # Closing the socket ends the shift's stream; the manager should not see a frozen frame.
    assert edge.get("/workers/live").json()["feeds"] == []


def test_streaming_an_unknown_worker_yields_an_empty_response_not_an_error(edge):
    """A manager opening a feed for someone who just stopped streaming gets nothing, not a 500."""

    with edge.stream("GET", "/workers/nobody/stream") as relay:
        assert relay.status_code == 200


def test_edge_stats_report_who_is_streaming(edge):
    with edge.websocket_connect("/ws/video?worker_id=w-7") as sock:
        sock.send_bytes(_jpeg())
        _recv(sock)
        stats = edge.get("/stats").json()

    assert stats["worker_feeds"]["streaming"] == 1
    assert stats["worker_feeds"]["workers"] == ["w-7"]


def test_a_blank_worker_id_is_treated_as_anonymous(edge):
    """`?worker_id=` from a signed-out app must not create a nameless feed."""

    with edge.websocket_connect("/ws/video?worker_id=%20") as sock:
        sock.send_bytes(_jpeg())
        _recv(sock)
        assert edge.get("/workers/live").json()["feeds"] == []
