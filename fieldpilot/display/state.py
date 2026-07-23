"""Shared live state between the pipeline (producer) and the web GUI (consumer).

The pipeline writes the latest annotated JPEG, a rolling stats snapshot, and hazard events; the
FastAPI routes read them. A single lock guards all of it — writes are tiny (a bytes swap and a dict
copy) so contention is negligible.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class LiveState:
    def __init__(self, max_events: int = 60):
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._stats: dict = {}
        self._events: deque[dict] = deque(maxlen=max_events)
        self.running = True
        self.started_at = time.time()

    def update_frame(self, jpeg: bytes, stats: dict) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._stats = stats

    def add_event(self, event: dict) -> None:
        with self._lock:
            self._events.appendleft(event)

    def get_jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "stats": dict(self._stats),
                "events": list(self._events),
                "uptime_s": round(time.time() - self.started_at, 1),
                "running": self.running,
            }
