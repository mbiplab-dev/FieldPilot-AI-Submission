"""Durable event storage — every event lands in a database before anything acts on it.

Primary backend: PostgreSQL via async SQLAlchemy (lazy imports — no hard dependency at
import time). Fallback: stdlib SQLite so dev/tests run with zero infrastructure while
keeping the exact same repository interface.

    events.backend: postgres | sqlite        (config.yaml)
    events.database_url: postgresql+psycopg://user:pass@host:5432/fieldpilot
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from fieldpilot.events.schema import Event
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.events.store")


class EventRepository(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def save_event(self, event: Event) -> None: ...
    async def list_events(
        self,
        *,
        event_type: str | None = None,
        worker_id: str | None = None,
        zone: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...
    async def count_by_type(self, since: float | None = None) -> dict[str, int]: ...


# ----------------------------------------------------------------------------- SQLite (stdlib)


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bus_events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    worker_id   TEXT,
    camera_id   TEXT NOT NULL,
    zone        TEXT,
    ts          REAL NOT NULL,
    confidence  REAL NOT NULL,
    severity    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    image_url   TEXT,
    video_url   TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bus_events_type   ON bus_events(event_type);
CREATE INDEX IF NOT EXISTS idx_bus_events_worker ON bus_events(worker_id);
CREATE INDEX IF NOT EXISTS idx_bus_events_ts     ON bus_events(ts);
"""


class SQLiteEventRepository:
    """Zero-dependency repository. Mirrors the Postgres schema 1:1."""

    def __init__(self, path: str = "data/events.db") -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SQLITE_SCHEMA)
            self._conn.commit()

    async def stop(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.close()
            self._conn = None

    async def save_event(self, event: Event) -> None:
        assert self._conn is not None, "repository not started"
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO bus_events
                   (event_id, event_type, worker_id, camera_id, zone, ts, confidence,
                    severity, payload, image_url, video_url, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id,
                    event.event_type.value,
                    event.worker_id,
                    event.camera_id,
                    event.zone,
                    event.timestamp.timestamp(),
                    event.confidence,
                    event.severity.value,
                    json.dumps(event.payload),
                    event.image_url,
                    event.video_url,
                    time.time(),
                ),
            )
            self._conn.commit()

    async def list_events(
        self,
        *,
        event_type: str | None = None,
        worker_id: str | None = None,
        zone: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None, "repository not started"
        clauses, params = [], []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if worker_id:
            clauses.append("worker_id = ?")
            params.append(worker_id)
        if zone:
            clauses.append("zone = ?")
            params.append(zone)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM bus_events{where} ORDER BY ts DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [_sqlite_row_to_dict(r) for r in rows]

    async def count_by_type(self, since: float | None = None) -> dict[str, int]:
        assert self._conn is not None, "repository not started"
        q = "SELECT event_type, COUNT(*) c FROM bus_events"
        params: tuple = ()
        if since is not None:
            q += " WHERE ts >= ?"
            params = (since,)
        q += " GROUP BY event_type"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return {r["event_type"]: r["c"] for r in rows}


def _sqlite_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": r["event_id"],
        "event_type": r["event_type"],
        "worker_id": r["worker_id"],
        "camera_id": r["camera_id"],
        "zone": r["zone"],
        "timestamp": r["ts"],
        "confidence": r["confidence"],
        "severity": r["severity"],
        "payload": json.loads(r["payload"] or "{}"),
        "image_url": r["image_url"],
        "video_url": r["video_url"],
    }


# --------------------------------------------------------------------------- Postgres (async)


class PostgresEventRepository:
    """Async SQLAlchemy + psycopg repository. Lazy-imports so the package is optional."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._engine = None
        self._session_factory = None
        self._table = None

    async def start(self) -> None:
        from sqlalchemy import Column, Float, MetaData, String, Table, Text
        from sqlalchemy.ext.asyncio import create_async_engine

        self._engine = create_async_engine(self.database_url, pool_pre_ping=True)
        metadata = MetaData()
        self._table = Table(
            "bus_events",
            metadata,
            Column("event_id", String(64), primary_key=True),
            Column("event_type", String(32), nullable=False, index=True),
            Column("worker_id", String(64), index=True),
            Column("camera_id", String(64), nullable=False),
            Column("zone", String(64), index=True),
            Column("ts", Float, nullable=False, index=True),
            Column("confidence", Float, nullable=False),
            Column("severity", String(16), nullable=False),
            Column("payload", Text, nullable=False),
            Column("image_url", Text),
            Column("video_url", Text),
            Column("created_at", Float, nullable=False),
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def stop(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def save_event(self, event: Event) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        assert self._engine is not None, "repository not started"
        values = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "worker_id": event.worker_id,
            "camera_id": event.camera_id,
            "zone": event.zone,
            "ts": event.timestamp.timestamp(),
            "confidence": event.confidence,
            "severity": event.severity.value,
            "payload": json.dumps(event.payload),
            "image_url": event.image_url,
            "video_url": event.video_url,
            "created_at": time.time(),
        }
        stmt = pg_insert(self._table).values(**values).on_conflict_do_nothing(
            index_elements=["event_id"]
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def list_events(
        self,
        *,
        event_type: str | None = None,
        worker_id: str | None = None,
        zone: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        from sqlalchemy import select

        assert self._engine is not None, "repository not started"
        t = self._table
        stmt = select(t).order_by(t.c.ts.desc()).limit(limit)
        if event_type:
            stmt = stmt.where(t.c.event_type == event_type)
        if worker_id:
            stmt = stmt.where(t.c.worker_id == worker_id)
        if zone:
            stmt = stmt.where(t.c.zone == zone)
        if since is not None:
            stmt = stmt.where(t.c.ts >= since)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [_pg_row_to_dict(r) for r in rows]

    async def count_by_type(self, since: float | None = None) -> dict[str, int]:
        from sqlalchemy import func, select

        assert self._engine is not None, "repository not started"
        t = self._table
        stmt = select(t.c.event_type, func.count()).group_by(t.c.event_type)
        if since is not None:
            stmt = stmt.where(t.c.ts >= since)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return {r[0]: int(r[1]) for r in rows}


def _pg_row_to_dict(r: Any) -> dict[str, Any]:
    payload = r["payload"]
    return {
        "event_id": r["event_id"],
        "event_type": r["event_type"],
        "worker_id": r["worker_id"],
        "camera_id": r["camera_id"],
        "zone": r["zone"],
        "timestamp": r["ts"],
        "confidence": r["confidence"],
        "severity": r["severity"],
        "payload": json.loads(payload) if isinstance(payload, str) else (payload or {}),
        "image_url": r["image_url"],
        "video_url": r["video_url"],
    }


# ------------------------------------------------------------------------------ factory


def create_repository(backend: str = "sqlite", database_url: str = "") -> EventRepository:
    if backend == "postgres" and database_url:
        try:
            import sqlalchemy  # noqa: F401

            return PostgresEventRepository(database_url)
        except ImportError:
            log.warning("sqlalchemy unavailable — falling back to SQLite event repository")
    return SQLiteEventRepository(database_url or "data/events.db")
