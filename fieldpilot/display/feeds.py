"""Live camera feeds, one per worker whose phone is streaming.

This is the "Pocket Mobile Phone Edge Node" of the architecture graph made real: the worker's phone
is the capture device, its frames run through the same safety pipeline as any other source, and the
site manager watches the result. The registry is the hand-off point between the two — the video
socket writes the newest annotated frame, the manager's MJPEG response reads it.

Deliberately **latest-frame-wins with no queue**, mirroring `LatestFrame` and `VideoSource`: a
stale frame is worthless for safety, and buffering would trade the one property that matters
(freshness) for smoothness nobody asked for. A slow viewer therefore skips frames rather than
dragging the whole feed backwards in time.

Feeds are held only in memory. This is a live view, not a recording — nothing here is a durable
record, and the alerts raised from these frames are what actually persist.
"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

#: A feed with no frame for this long is treated as gone. Comfortably longer than the phone's
#: slowest duty-cycled interval, so a battery-saving worker is not reported as disconnected.
DEFAULT_STALE_AFTER_S = 12.0


@dataclass
class WorkerFeed:
    """The newest frame from one worker's phone, plus how that stream is behaving."""

    worker_id: str
    zone: str | None = None
    display_name: str | None = None
    started_at: float = field(default_factory=time.time)
    last_frame_at: float = 0.0
    frames: int = 0
    hazards: int = 0
    #: Rolling frame rate, so the dashboard can show a feed that is technically alive but crawling.
    fps: float = 0.0
    width: int = 0
    height: int = 0
    jpeg: bytes | None = None
    raw_jpeg: bytes | None = None
    detections: list[dict[str, Any]] = field(default_factory=list)

    def describe(self, *, now: float | None = None, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> dict[str, Any]:
        """JSON-safe summary. Excludes the frame bytes — those go over the MJPEG route."""

        now = time.time() if now is None else now
        age = now - self.last_frame_at if self.last_frame_at else None
        return {
            "worker_id": self.worker_id,
            "zone": self.zone,
            "display_name": self.display_name,
            "started_at": self.started_at,
            "last_frame_at": self.last_frame_at or None,
            "age_s": round(age, 2) if age is not None else None,
            "live": age is not None and age <= stale_after_s,
            "frames": self.frames,
            "hazards": self.hazards,
            "fps": round(self.fps, 1),
            "width": self.width,
            "height": self.height,
        }


class FeedRegistry:
    """Every worker currently streaming, keyed by worker id.

    Guarded by one lock: writes are a bytes swap plus a few counters, so contention is negligible
    (the same reasoning as `LiveState`).
    """

    def __init__(self, *, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> None:
        self._lock = threading.Lock()
        self._feeds: dict[str, WorkerFeed] = {}
        self.stale_after_s = float(stale_after_s)

    # -- producer side ---------------------------------------------------------

    def open(self, worker_id: str, *, zone: str | None = None,
             display_name: str | None = None) -> WorkerFeed:
        """Register a worker as streaming. Reconnecting keeps the existing feed's counters."""

        with self._lock:
            feed = self._feeds.get(worker_id)
            if feed is None:
                feed = WorkerFeed(worker_id=worker_id)
                self._feeds[worker_id] = feed
            # A worker can change zone mid-stream by walking into one; keep the newest.
            feed.zone = zone if zone is not None else feed.zone
            feed.display_name = display_name or feed.display_name
            return feed

    def publish(self, worker_id: str, jpeg: bytes, *, width: int, height: int,
                hazards: int = 0, raw_jpeg: bytes | None = None,
                detections: list[dict[str, Any]] | None = None) -> None:
        """Record the newest annotated frame plus an optional pristine capture candidate."""

        now = time.time()
        with self._lock:
            feed = self._feeds.get(worker_id)
            if feed is None:
                return  # stream closed between decode and publish — nothing to update
            if feed.last_frame_at:
                gap = now - feed.last_frame_at
                if gap > 0:
                    # Exponential moving average: one slow frame should not erase the real rate,
                    # and one fast frame should not claim a rate the stream is not sustaining.
                    instant = 1.0 / gap
                    feed.fps = instant if feed.fps == 0 else (feed.fps * 0.7 + instant * 0.3)
            feed.jpeg = jpeg
            if raw_jpeg is not None:
                feed.raw_jpeg = raw_jpeg
            if detections is not None:
                feed.detections = deepcopy(detections)
            feed.last_frame_at = now
            feed.frames += 1
            feed.hazards += int(hazards)
            feed.width, feed.height = int(width), int(height)

    def close(self, worker_id: str) -> None:
        with self._lock:
            self._feeds.pop(worker_id, None)

    # -- consumer side ---------------------------------------------------------

    def frame(self, worker_id: str) -> bytes | None:
        with self._lock:
            feed = self._feeds.get(worker_id)
            return feed.jpeg if feed else None

    def get(self, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            feed = self._feeds.get(worker_id)
            return feed.describe(stale_after_s=self.stale_after_s) if feed else None

    def capture(self, worker_id: str) -> dict[str, Any] | None:
        """A consistent pristine frame + draft detections for an intentional manager capture."""

        with self._lock:
            feed = self._feeds.get(worker_id)
            if feed is None or feed.raw_jpeg is None:
                return None
            return {
                **feed.describe(stale_after_s=self.stale_after_s),
                "jpeg": bytes(feed.raw_jpeg),
                "detections": deepcopy(feed.detections),
            }

    def list(self) -> list[dict[str, Any]]:
        """Every known feed, most recently active first."""

        now = time.time()
        with self._lock:
            feeds = [f.describe(now=now, stale_after_s=self.stale_after_s)
                     for f in self._feeds.values()]
        feeds.sort(key=lambda f: f["last_frame_at"] or 0, reverse=True)
        return feeds

    def stats(self) -> dict[str, Any]:
        feeds = self.list()
        return {
            "streaming": sum(1 for f in feeds if f["live"]),
            "known": len(feeds),
            "workers": [f["worker_id"] for f in feeds if f["live"]],
        }

    def evict_stale(self, *, older_than_s: float = 300.0) -> list[str]:
        """Drop feeds nothing has written to in a long time, so a phone that vanished mid-shift
        does not linger in the dashboard forever. Returns the ids removed."""

        cutoff = time.time() - float(older_than_s)
        with self._lock:
            gone = [wid for wid, f in self._feeds.items()
                    if (f.last_frame_at or f.started_at) < cutoff]
            for wid in gone:
                self._feeds.pop(wid, None)
        return gone
