"""SQLite event store.

Local, durable record of every hazard event and alert. Doubles as the offline store-and-forward
queue (Milestone 3): rows carry a `synced` flag so the flusher can reconcile with the central API
after a network drop. Access is guarded by a lock so the capture thread, inference loop, and
FastAPI workers can share one connection safely.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from fieldpilot.core.types import HazardEvent, HazardType, Severity

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    hazard_type   TEXT NOT NULL,
    severity      TEXT NOT NULL,
    message       TEXT NOT NULL,
    frame_index   INTEGER NOT NULL,
    ts_monotonic  REAL NOT NULL,
    ts_wall       REAL NOT NULL,
    track_id      INTEGER,
    bbox          TEXT,
    meta          TEXT,
    alerted       INTEGER NOT NULL DEFAULT 0,
    synced        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_synced ON events(synced);
CREATE INDEX IF NOT EXISTS idx_events_type   ON events(hazard_type);

CREATE TABLE IF NOT EXISTS feedback (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL,
    decision    TEXT NOT NULL,          -- approve | reject
    reviewer    TEXT,
    created_at  REAL NOT NULL,
    FOREIGN KEY(event_id) REFERENCES events(id)
);
"""


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record_event(self, event: HazardEvent, alerted: bool = False) -> None:
        """Idempotent on the event id — safe to replay from the offline queue."""

        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO events
                   (id, hazard_type, severity, message, frame_index, ts_monotonic, ts_wall,
                    track_id, bbox, meta, alerted, synced, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (
                    event.id,
                    event.hazard_type.value,
                    event.severity.value,
                    event.message,
                    event.frame_index,
                    event.ts_monotonic,
                    event.ts_wall,
                    event.track_id,
                    json.dumps(event.bbox) if event.bbox is not None else None,
                    json.dumps(event.meta),
                    int(alerted),
                    time.time(),
                ),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def counts_by_type(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT hazard_type, COUNT(*) c FROM events GROUP BY hazard_type"
            ).fetchall()
        return {r["hazard_type"]: r["c"] for r in rows}

    def recent(self, n: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [self._row_to_event_dict(r) for r in rows]

    def unsynced(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE synced = 0 ORDER BY created_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_event_dict(r) for r in rows]

    def mark_synced(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE events SET synced = 1 WHERE id = ?", [(eid,) for eid in event_ids]
            )
            self._conn.commit()

    def add_feedback(self, feedback_id: str, event_id: str, decision: str, reviewer: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO feedback (id, event_id, decision, reviewer, created_at) "
                "VALUES (?,?,?,?,?)",
                (feedback_id, event_id, decision, reviewer, time.time()),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_event_dict(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "hazard_type": r["hazard_type"],
            "severity": r["severity"],
            "message": r["message"],
            "frame_index": r["frame_index"],
            "ts_monotonic": r["ts_monotonic"],
            "ts_wall": r["ts_wall"],
            "track_id": r["track_id"],
            "bbox": json.loads(r["bbox"]) if r["bbox"] else None,
            "meta": json.loads(r["meta"]) if r["meta"] else {},
            "alerted": bool(r["alerted"]),
            "synced": bool(r["synced"]),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def event_from_row(row: dict) -> HazardEvent:
    """Rehydrate a HazardEvent from a stored row (used by the offline flusher)."""

    return HazardEvent(
        hazard_type=HazardType(row["hazard_type"]),
        severity=Severity(row["severity"]),
        message=row["message"],
        frame_index=row["frame_index"],
        ts_monotonic=row["ts_monotonic"],
        track_id=row["track_id"],
        bbox=tuple(row["bbox"]) if row["bbox"] else None,
        meta=row.get("meta", {}),
        id=row["id"],
        ts_wall=row["ts_wall"],
    )
