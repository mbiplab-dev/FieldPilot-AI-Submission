"""Durable platform storage: alerts, rules, notifications, RFIs, inspections.

Production backend is PostgreSQL (async SQLAlchemy, lazy imports). Dev/tests use stdlib
SQLite with the identical repository interface — engine code never branches on infra.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.backend.store")


class PlatformStore(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def upsert_alert(self, alert: dict[str, Any]) -> None: ...
    async def get_alert(self, alert_id: str) -> dict[str, Any] | None: ...
    async def list_alerts(
        self,
        *,
        state: str | None = None,
        severity: str | None = None,
        worker_id: str | None = None,
        zone: str | None = None,
        event_type: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    async def put_rule(self, rule: dict[str, Any]) -> None: ...
    async def get_rule(self, rule_id: str) -> dict[str, Any] | None: ...
    async def delete_rule(self, rule_id: str) -> bool: ...
    async def list_rules(self) -> list[dict[str, Any]]: ...

    async def save_notification(self, note: dict[str, Any]) -> None: ...
    async def list_notifications(self, *, limit: int = 200) -> list[dict[str, Any]]: ...

    async def save_rfi(self, rfi: dict[str, Any]) -> None: ...
    async def get_rfi(self, rfi_id: str) -> dict[str, Any] | None: ...
    async def update_rfi(
        self, rfi_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None: ...
    async def list_rfis(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    async def save_inspection(self, insp: dict[str, Any]) -> None: ...
    async def get_inspection(self, inspection_id: str) -> dict[str, Any] | None: ...
    async def update_inspection(
        self, inspection_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None: ...
    async def list_inspections(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]: ...


# ----------------------------------------------------------------------------- SQLite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_id    TEXT PRIMARY KEY,
    dedup_key   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    worker_id   TEXT,
    camera_id   TEXT,
    zone        TEXT,
    severity    TEXT NOT NULL,
    state       TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 1,
    confidence  REAL NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    resolved_at REAL,
    suppressed_at REAL,
    message     TEXT,
    payload     TEXT,
    image_url   TEXT,
    video_url   TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_state  ON alerts(state);
CREATE INDEX IF NOT EXISTS idx_alerts_worker ON alerts(worker_id);
CREATE INDEX IF NOT EXISTS idx_alerts_zone   ON alerts(zone);
CREATE INDEX IF NOT EXISTS idx_alerts_type   ON alerts(event_type);

CREATE TABLE IF NOT EXISTS rules (
    rule_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 100,
    event_types TEXT NOT NULL DEFAULT '[]',
    conditions  TEXT NOT NULL DEFAULT '[]',
    action      TEXT NOT NULL DEFAULT '{}',
    cooldown_s  REAL NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    dedup_key   TEXT NOT NULL,
    channel     TEXT NOT NULL,
    subject     TEXT,
    body        TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    attempts    INTEGER NOT NULL DEFAULT 0,
    alert_id    TEXT,
    created_at  REAL NOT NULL,
    sent_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_notifications_dedup ON notifications(dedup_key);

CREATE TABLE IF NOT EXISTS rfis (
    rfi_id      TEXT PRIMARY KEY,
    event_id    TEXT,
    title       TEXT,
    summary     TEXT,
    priority    TEXT,
    zone        TEXT,
    payload     TEXT,
    created_at  REAL NOT NULL,
    -- an RFI is drafted by the LLM but filed for human review; these carry that review
    status      TEXT NOT NULL DEFAULT 'pending_review',
    body        TEXT,
    citation    TEXT,
    reviewer    TEXT,
    notes       TEXT,
    reviewed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_rfis_status ON rfis(status);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    event_id    TEXT,
    priority    TEXT,
    zone        TEXT,
    message     TEXT,
    status      TEXT NOT NULL DEFAULT 'requested',
    created_at  REAL NOT NULL,
    notes       TEXT,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_inspections_status ON inspections(status);
"""

#: columns added after the first release — applied to already-created tables on start
_MIGRATIONS: dict[str, dict[str, str]] = {
    "rfis": {
        "status": "TEXT NOT NULL DEFAULT 'pending_review'",
        "body": "TEXT",
        "citation": "TEXT",
        "reviewer": "TEXT",
        "notes": "TEXT",
        "reviewed_at": "REAL",
    },
    "inspections": {
        "notes": "TEXT",
        "completed_at": "REAL",
    },
}

_ALERT_COLS = (
    "alert_id", "dedup_key", "event_type", "worker_id", "camera_id", "zone", "severity",
    "state", "hit_count", "confidence", "first_seen", "last_seen", "resolved_at",
    "suppressed_at", "message", "payload", "image_url", "video_url",
)


class SQLitePlatformStore:
    def __init__(self, path: str = "data/platform.db") -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns a database created by an earlier build is missing."""

        assert self._conn is not None
        for table, columns in _MIGRATIONS.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name in have:
                    continue
                # SQLite cannot add a NOT NULL column without a default; ours all have one
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                log.info("migrated %s: added column %s", table, name)

    async def stop(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.close()
            self._conn = None

    # -- alerts ---------------------------------------------------------------

    async def upsert_alert(self, alert: dict[str, Any]) -> None:
        assert self._conn is not None
        row = {c: alert.get(c) for c in _ALERT_COLS}
        row["payload"] = json.dumps(row.get("payload") or {})
        with self._lock:
            self._conn.execute(
                f"""INSERT INTO alerts ({",".join(_ALERT_COLS)})
                    VALUES ({",".join("?" for _ in _ALERT_COLS)})
                    ON CONFLICT(alert_id) DO UPDATE SET
                      state=excluded.state, hit_count=excluded.hit_count,
                      confidence=excluded.confidence, last_seen=excluded.last_seen,
                      resolved_at=excluded.resolved_at, suppressed_at=excluded.suppressed_at,
                      severity=excluded.severity, message=excluded.message,
                      payload=excluded.payload, image_url=excluded.image_url,
                      video_url=excluded.video_url""",
                tuple(row[c] for c in _ALERT_COLS),
            )
            self._conn.commit()

    async def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)
            ).fetchone()
        return _alert_row(r) if r else None

    async def list_alerts(
        self,
        *,
        state: str | None = None,
        severity: str | None = None,
        worker_id: str | None = None,
        zone: str | None = None,
        event_type: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        clauses, params = [], []
        for col, val in (("state", state), ("severity", severity), ("worker_id", worker_id),
                         ("zone", zone), ("event_type", event_type)):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            clauses.append("last_seen >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM alerts{where} ORDER BY last_seen DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [_alert_row(r) for r in rows]

    # -- rules -----------------------------------------------------------------

    async def put_rule(self, rule: dict[str, Any]) -> None:
        assert self._conn is not None
        with self._lock:
            self._conn.execute(
                """INSERT INTO rules
                   (rule_id, name, enabled, priority, event_types, conditions, action,
                    cooldown_s, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(rule_id) DO UPDATE SET
                     name=excluded.name, enabled=excluded.enabled, priority=excluded.priority,
                     event_types=excluded.event_types, conditions=excluded.conditions,
                     action=excluded.action, cooldown_s=excluded.cooldown_s""",
                (
                    rule["rule_id"], rule["name"], int(rule.get("enabled", True)),
                    int(rule.get("priority", 100)), json.dumps(rule.get("event_types") or []),
                    json.dumps(rule.get("conditions") or []), json.dumps(rule.get("action") or {}),
                    float(rule.get("cooldown_s", 0.0)), float(rule.get("created_at", time.time())),
                ),
            )
            self._conn.commit()

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        with self._lock:
            r = self._conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
        return _rule_row(r) if r else None

    async def delete_rule(self, rule_id: str) -> bool:
        assert self._conn is not None
        with self._lock:
            cur = self._conn.execute("DELETE FROM rules WHERE rule_id = ?", (rule_id,))
            self._conn.commit()
        return cur.rowcount > 0

    async def list_rules(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute("SELECT * FROM rules ORDER BY priority DESC").fetchall()
        return [_rule_row(r) for r in rows]

    # -- notifications -----------------------------------------------------------

    async def save_notification(self, note: dict[str, Any]) -> None:
        assert self._conn is not None
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO notifications
                   (notification_id, dedup_key, channel, subject, body, status, attempts,
                    alert_id, created_at, sent_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    note["notification_id"], note["dedup_key"], note["channel"],
                    note.get("subject"), note.get("body"), note.get("status", "queued"),
                    int(note.get("attempts", 0)), note.get("alert_id"),
                    float(note.get("created_at", time.time())), note.get("sent_at"),
                ),
            )
            self._conn.commit()

    async def list_notifications(self, *, limit: int = 200) -> list[dict[str, Any]]:
        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- RFIs / inspections -------------------------------------------------------

    async def save_rfi(self, rfi: dict[str, Any]) -> None:
        assert self._conn is not None
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO rfis
                   (rfi_id, event_id, title, summary, priority, zone, payload, created_at,
                    status, body, citation, reviewer, notes, reviewed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rfi["rfi_id"], rfi.get("event_id"), rfi.get("title"), rfi.get("summary"),
                    rfi.get("priority"), rfi.get("zone"), json.dumps(rfi.get("payload") or {}),
                    float(rfi.get("created_at", time.time())),
                    rfi.get("status", "pending_review"), rfi.get("body"), rfi.get("citation"),
                    rfi.get("reviewer"), rfi.get("notes"), rfi.get("reviewed_at"),
                ),
            )
            self._conn.commit()

    async def get_rfi(self, rfi_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        with self._lock:
            r = self._conn.execute("SELECT * FROM rfis WHERE rfi_id = ?", (rfi_id,)).fetchone()
        return _rfi_row(r) if r else None

    async def update_rfi(self, rfi_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = ("status", "reviewer", "notes", "reviewed_at", "priority", "body", "citation")
        sets = {k: v for k, v in changes.items() if k in allowed}
        if not sets:
            return await self.get_rfi(rfi_id)
        assert self._conn is not None
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE rfis SET {', '.join(f'{k} = ?' for k in sets)} WHERE rfi_id = ?",  # noqa: S608
                (*sets.values(), rfi_id),
            )
            self._conn.commit()
        return await self.get_rfi(rfi_id) if cur.rowcount else None

    async def list_rfis(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        where, params = ("", [])
        if status:
            where, params = (" WHERE status = ?", [status])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM rfis{where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [_rfi_row(r) for r in rows]

    async def save_inspection(self, insp: dict[str, Any]) -> None:
        assert self._conn is not None
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO inspections
                   (inspection_id, event_id, priority, zone, message, status, created_at,
                    notes, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    insp["inspection_id"], insp.get("event_id"), insp.get("priority"),
                    insp.get("zone"), insp.get("message"), insp.get("status", "requested"),
                    float(insp.get("created_at", time.time())),
                    insp.get("notes"), insp.get("completed_at"),
                ),
            )
            self._conn.commit()

    async def get_inspection(self, inspection_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,)
            ).fetchone()
        return dict(r) if r else None

    async def update_inspection(
        self, inspection_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = ("status", "notes", "completed_at", "priority")
        sets = {k: v for k, v in changes.items() if k in allowed}
        if not sets:
            return await self.get_inspection(inspection_id)
        assert self._conn is not None
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE inspections SET {', '.join(f'{k} = ?' for k in sets)} "  # noqa: S608
                "WHERE inspection_id = ?",
                (*sets.values(), inspection_id),
            )
            self._conn.commit()
        return await self.get_inspection(inspection_id) if cur.rowcount else None

    async def list_inspections(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        where, params = ("", [])
        if status:
            where, params = (" WHERE status = ?", [status])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM inspections{where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def _alert_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["payload"] = json.loads(d.get("payload") or "{}")
    return d


def _rfi_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["payload"] = json.loads(d.get("payload") or "{}")
    return d


def _rule_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    d["event_types"] = json.loads(d["event_types"] or "[]")
    d["conditions"] = json.loads(d["conditions"] or "[]")
    d["action"] = json.loads(d["action"] or "{}")
    return d


# ----------------------------------------------------------------------------- Postgres


class PostgresPlatformStore:
    """Same interface on async SQLAlchemy + psycopg. Tables created on start."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._engine = None
        self._t: dict[str, Any] = {}

    async def start(self) -> None:
        from sqlalchemy import (
            Boolean,
            Column,
            Float,
            Integer,
            MetaData,
            String,
            Table,
            Text,
        )
        from sqlalchemy.ext.asyncio import create_async_engine

        self._engine = create_async_engine(self.database_url, pool_pre_ping=True)
        md = MetaData()
        self._t["alerts"] = Table(
            "alerts", md,
            Column("alert_id", String(64), primary_key=True),
            Column("dedup_key", Text, nullable=False),
            Column("event_type", String(32), nullable=False, index=True),
            Column("worker_id", String(64), index=True),
            Column("camera_id", String(64)),
            Column("zone", String(64), index=True),
            Column("severity", String(16), nullable=False),
            Column("state", String(16), nullable=False, index=True),
            Column("hit_count", Integer, nullable=False, default=1),
            Column("confidence", Float, nullable=False, default=0),
            Column("first_seen", Float, nullable=False),
            Column("last_seen", Float, nullable=False, index=True),
            Column("resolved_at", Float),
            Column("suppressed_at", Float),
            Column("message", Text),
            Column("payload", Text),
            Column("image_url", Text),
            Column("video_url", Text),
        )
        self._t["rules"] = Table(
            "rules", md,
            Column("rule_id", String(64), primary_key=True),
            Column("name", Text, nullable=False),
            Column("enabled", Boolean, nullable=False, default=True),
            Column("priority", Integer, nullable=False, default=100),
            Column("event_types", Text, nullable=False, default="[]"),
            Column("conditions", Text, nullable=False, default="[]"),
            Column("action", Text, nullable=False, default="{}"),
            Column("cooldown_s", Float, nullable=False, default=0),
            Column("created_at", Float, nullable=False),
        )
        self._t["notifications"] = Table(
            "notifications", md,
            Column("notification_id", String(64), primary_key=True),
            Column("dedup_key", Text, nullable=False, index=True),
            Column("channel", String(32), nullable=False),
            Column("subject", Text),
            Column("body", Text),
            Column("status", String(16), nullable=False, default="queued"),
            Column("attempts", Integer, nullable=False, default=0),
            Column("alert_id", String(64)),
            Column("created_at", Float, nullable=False),
            Column("sent_at", Float),
        )
        self._t["rfis"] = Table(
            "rfis", md,
            Column("rfi_id", String(64), primary_key=True),
            Column("event_id", String(64)),
            Column("title", Text),
            Column("summary", Text),
            Column("priority", String(16)),
            Column("zone", String(64)),
            Column("payload", Text),
            Column("created_at", Float, nullable=False),
            Column("status", String(24), nullable=False, default="pending_review", index=True),
            Column("body", Text),
            Column("citation", Text),
            Column("reviewer", String(64)),
            Column("notes", Text),
            Column("reviewed_at", Float),
        )
        self._t["inspections"] = Table(
            "inspections", md,
            Column("inspection_id", String(64), primary_key=True),
            Column("event_id", String(64)),
            Column("priority", String(16)),
            Column("zone", String(64)),
            Column("message", Text),
            Column("status", String(16), nullable=False, default="requested", index=True),
            Column("created_at", Float, nullable=False),
            Column("notes", Text),
            Column("completed_at", Float),
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(md.create_all)
            await self._migrate(conn)

    @staticmethod
    async def _migrate(conn: Any) -> None:
        """`create_all` skips existing tables, so newer columns are added explicitly."""

        from sqlalchemy import text as sa_text

        pg_ddl = {
            "rfis": {
                "status": "TEXT NOT NULL DEFAULT 'pending_review'", "body": "TEXT",
                "citation": "TEXT", "reviewer": "TEXT", "notes": "TEXT",
                "reviewed_at": "DOUBLE PRECISION",
            },
            "inspections": {"notes": "TEXT", "completed_at": "DOUBLE PRECISION"},
        }
        for table, columns in pg_ddl.items():
            for name, ddl in columns.items():
                await conn.execute(sa_text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"
                ))

    async def stop(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def upsert_alert(self, alert: dict[str, Any]) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        t = self._t["alerts"]
        row = {c: alert.get(c) for c in _ALERT_COLS}
        row["payload"] = json.dumps(row.get("payload") or {})
        stmt = pg_insert(t).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["alert_id"],
            set_={c: row[c] for c in _ALERT_COLS if c not in ("alert_id", "first_seen")},
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        t = self._t["alerts"]
        async with self._engine.connect() as conn:
            r = (await conn.execute(select(t).where(t.c.alert_id == alert_id))).mappings().first()
        if r is None:
            return None
        d = dict(r)
        d["payload"] = json.loads(d.get("payload") or "{}")
        return d

    async def list_alerts(self, *, state=None, severity=None, worker_id=None, zone=None,
                          event_type=None, since=None, limit=200) -> list[dict[str, Any]]:
        from sqlalchemy import select

        t = self._t["alerts"]
        stmt = select(t).order_by(t.c.last_seen.desc()).limit(limit)
        for col, val in (("state", state), ("severity", severity), ("worker_id", worker_id),
                         ("zone", zone), ("event_type", event_type)):
            if val:
                stmt = stmt.where(t.c[col] == val)
        if since is not None:
            stmt = stmt.where(t.c.last_seen >= since)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.get("payload") or "{}")
            out.append(d)
        return out

    async def put_rule(self, rule: dict[str, Any]) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        t = self._t["rules"]
        row = {
            "rule_id": rule["rule_id"], "name": rule["name"],
            "enabled": bool(rule.get("enabled", True)),
            "priority": int(rule.get("priority", 100)),
            "event_types": json.dumps(rule.get("event_types") or []),
            "conditions": json.dumps(rule.get("conditions") or []),
            "action": json.dumps(rule.get("action") or {}),
            "cooldown_s": float(rule.get("cooldown_s", 0.0)),
            "created_at": float(rule.get("created_at", time.time())),
        }
        stmt = pg_insert(t).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["rule_id"],
            set_={k: v for k, v in row.items() if k not in ("rule_id", "created_at")},
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        t = self._t["rules"]
        async with self._engine.connect() as conn:
            r = (await conn.execute(select(t).where(t.c.rule_id == rule_id))).mappings().first()
        return _pg_rule(r) if r else None

    async def delete_rule(self, rule_id: str) -> bool:
        from sqlalchemy import delete

        t = self._t["rules"]
        async with self._engine.begin() as conn:
            res = await conn.execute(delete(t).where(t.c.rule_id == rule_id))
        return (res.rowcount or 0) > 0

    async def list_rules(self) -> list[dict[str, Any]]:
        from sqlalchemy import select

        t = self._t["rules"]
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(t).order_by(t.c.priority.desc()))).mappings().all()
        return [_pg_rule(r) for r in rows]

    async def save_notification(self, note: dict[str, Any]) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        t = self._t["notifications"]
        stmt = pg_insert(t).values(
            notification_id=note["notification_id"], dedup_key=note["dedup_key"],
            channel=note["channel"], subject=note.get("subject"), body=note.get("body"),
            status=note.get("status", "queued"), attempts=int(note.get("attempts", 0)),
            alert_id=note.get("alert_id"), created_at=float(note.get("created_at", time.time())),
            sent_at=note.get("sent_at"),
        ).on_conflict_do_nothing(index_elements=["notification_id"])
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def list_notifications(self, *, limit: int = 200) -> list[dict[str, Any]]:
        from sqlalchemy import select

        t = self._t["notifications"]
        async with self._engine.connect() as conn:
            rows = (await conn.execute(
                select(t).order_by(t.c.created_at.desc()).limit(limit))).mappings().all()
        return [dict(r) for r in rows]

    async def save_rfi(self, rfi: dict[str, Any]) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        t = self._t["rfis"]
        stmt = pg_insert(t).values(
            rfi_id=rfi["rfi_id"], event_id=rfi.get("event_id"), title=rfi.get("title"),
            summary=rfi.get("summary"), priority=rfi.get("priority"), zone=rfi.get("zone"),
            payload=json.dumps(rfi.get("payload") or {}),
            created_at=float(rfi.get("created_at", time.time())),
            status=rfi.get("status", "pending_review"), body=rfi.get("body"),
            citation=rfi.get("citation"), reviewer=rfi.get("reviewer"),
            notes=rfi.get("notes"), reviewed_at=rfi.get("reviewed_at"),
        ).on_conflict_do_nothing(index_elements=["rfi_id"])
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def get_rfi(self, rfi_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        t = self._t["rfis"]
        async with self._engine.connect() as conn:
            r = (await conn.execute(select(t).where(t.c.rfi_id == rfi_id))).mappings().first()
        return {**dict(r), "payload": json.loads(r["payload"] or "{}")} if r else None

    async def update_rfi(self, rfi_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        from sqlalchemy import update

        allowed = ("status", "reviewer", "notes", "reviewed_at", "priority", "body", "citation")
        sets = {k: v for k, v in changes.items() if k in allowed}
        if not sets:
            return await self.get_rfi(rfi_id)
        t = self._t["rfis"]
        async with self._engine.begin() as conn:
            res = await conn.execute(update(t).where(t.c.rfi_id == rfi_id).values(**sets))
        return await self.get_rfi(rfi_id) if (res.rowcount or 0) > 0 else None

    async def list_rfis(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        from sqlalchemy import select

        t = self._t["rfis"]
        stmt = select(t).order_by(t.c.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(t.c.status == status)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in rows]

    async def save_inspection(self, insp: dict[str, Any]) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        t = self._t["inspections"]
        stmt = pg_insert(t).values(
            inspection_id=insp["inspection_id"], event_id=insp.get("event_id"),
            priority=insp.get("priority"), zone=insp.get("zone"), message=insp.get("message"),
            status=insp.get("status", "requested"),
            created_at=float(insp.get("created_at", time.time())),
            notes=insp.get("notes"), completed_at=insp.get("completed_at"),
        ).on_conflict_do_nothing(index_elements=["inspection_id"])
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def get_inspection(self, inspection_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        t = self._t["inspections"]
        async with self._engine.connect() as conn:
            r = (await conn.execute(
                select(t).where(t.c.inspection_id == inspection_id))).mappings().first()
        return dict(r) if r else None

    async def update_inspection(
        self, inspection_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        from sqlalchemy import update

        allowed = ("status", "notes", "completed_at", "priority")
        sets = {k: v for k, v in changes.items() if k in allowed}
        if not sets:
            return await self.get_inspection(inspection_id)
        t = self._t["inspections"]
        async with self._engine.begin() as conn:
            res = await conn.execute(
                update(t).where(t.c.inspection_id == inspection_id).values(**sets)
            )
        return await self.get_inspection(inspection_id) if (res.rowcount or 0) > 0 else None

    async def list_inspections(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        from sqlalchemy import select

        t = self._t["inspections"]
        stmt = select(t).order_by(t.c.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(t.c.status == status)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]


def _pg_rule(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    d["event_types"] = json.loads(d["event_types"] or "[]")
    d["conditions"] = json.loads(d["conditions"] or "[]")
    d["action"] = json.loads(d["action"] or "{}")
    return d


def create_platform_store(backend: str = "sqlite", database_url: str = "") -> PlatformStore:
    if backend == "postgres" and database_url:
        try:
            import sqlalchemy  # noqa: F401

            return PostgresPlatformStore(database_url)
        except ImportError:
            log.warning("sqlalchemy unavailable — falling back to SQLite platform store")
    return SQLitePlatformStore(database_url or "data/platform.db")
