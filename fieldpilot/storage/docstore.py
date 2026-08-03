"""A record store that speaks SQLite and PostgreSQL through one interface.

Each table declares its primary key and the scalar columns worth querying on; every other key
in a record is JSON-encoded into a `payload` column and merged back transparently on read. That
keeps new domain tables cheap to add without giving up SQL filtering on the fields that matter.

    ZONES = TableSpec("zones", key="zone_id", columns=(
        Column("name"), Column("project_id", indexed=True), Column("created_at", "real"),
    ))

    store = DocStore("sqlite", "data/platform.db")
    await store.start([ZONES])
    zones = store.table(ZONES)
    await zones.put({"zone_id": "z1", "name": "Level 3 East", "geojson": {...}})

`geojson` is not a declared column, so it round-trips through `payload`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.storage")

_SQLITE_TYPES = {"text": "TEXT", "real": "REAL", "int": "INTEGER", "bool": "INTEGER"}


@dataclass(frozen=True)
class Column:
    name: str
    type: str = "text"          # text | real | int | bool
    indexed: bool = False


@dataclass(frozen=True)
class TableSpec:
    name: str
    key: str
    columns: tuple[Column, ...] = ()
    order_by: str = "created_at"
    #: columns never overwritten by an upsert of an existing row
    immutable: tuple[str, ...] = ("created_at",)

    @property
    def column_names(self) -> tuple[str, ...]:
        return (self.key, *(c.name for c in self.columns), "payload")

    def split(self, record: dict[str, Any]) -> dict[str, Any]:
        """Fold undeclared keys into `payload`, coercing declared ones to their SQL type."""

        declared = {c.name: c for c in self.columns}
        row: dict[str, Any] = {self.key: record.get(self.key)}
        payload: dict[str, Any] = {}
        for k, v in record.items():
            if k == self.key or k == "payload":
                continue
            col = declared.get(k)
            if col is None:
                payload[k] = v
            elif col.type == "bool":
                row[k] = int(bool(v)) if v is not None else None
            elif col.type == "int":
                row[k] = int(v) if v is not None else None
            elif col.type == "real":
                row[k] = float(v) if v is not None else None
            else:
                row[k] = v if v is None else str(v)
        # a nested payload dict passed explicitly is merged, not shadowed
        if isinstance(record.get("payload"), dict):
            payload = {**record["payload"], **payload}
        for name in declared:
            row.setdefault(name, None)
        row["payload"] = json.dumps(payload)
        return row

    def merge(self, row: dict[str, Any]) -> dict[str, Any]:
        """Inverse of `split`: rebuild the record from columns + payload."""

        out = dict(row)
        payload = out.pop("payload", None)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except json.JSONDecodeError:
                payload = {}
        for c in self.columns:
            if c.type == "bool" and out.get(c.name) is not None:
                out[c.name] = bool(out[c.name])
        return {**(payload or {}), **out}


#: filter operators recognised in a `where` mapping's 2-tuple form
_OPS = ("in", "isnull", "gte")


def _where_sql(spec: TableSpec, where: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """Build a WHERE fragment. Values may be scalars, ('in', [...]) or ('isnull', bool).

    A `None` value means "no filter on this column" (convenient for optional query params);
    use `("isnull", True)` to actually match NULLs.
    """

    if not where:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for col, val in where.items():
        # a 2-tuple headed by a string is an operator pair; anything else is a plain value, so a
        # legitimate tuple value is not silently reinterpreted as a filter op
        if isinstance(val, tuple) and len(val) == 2 and isinstance(val[0], str):
            op, operand = val
            if op == "in":
                items = list(operand)
                if not items:
                    return " WHERE 1 = 0", []
                clauses.append(f"{col} IN ({','.join('?' for _ in items)})")
                params.extend(items)
                continue
            if op == "isnull":
                clauses.append(f"{col} IS {'NULL' if operand else 'NOT NULL'}")
                continue
            if op == "gte":
                clauses.append(f"{col} >= ?")
                params.append(operand)
                continue
            raise ValueError(f"unsupported filter op: {op!r}")
        if val is None:
            continue
        clauses.append(f"{col} = ?")
        params.append(val)
    if not clauses:
        return "", []
    _ = spec
    return " WHERE " + " AND ".join(clauses), params


class Table:
    """A `TableSpec` bound to a `DocStore`, so callers pass records rather than SQL."""

    def __init__(self, store: DocStore, spec: TableSpec) -> None:
        self._store = store
        self.spec = spec

    async def put(self, record: dict[str, Any]) -> dict[str, Any]:
        """Insert or **replace** the whole record. Columns absent from `record` become NULL —
        use `patch` to change a few fields of an existing row. Columns listed in
        `spec.immutable` (by default `created_at`) keep their original value."""

        record = dict(record)
        record.setdefault("created_at", time.time())
        await self._store._put(self.spec, record)
        return record

    async def get(self, key: str) -> dict[str, Any] | None:
        return await self._store._get(self.spec, key)

    async def delete(self, key: str) -> bool:
        return await self._store._delete(self.spec, key)

    async def list(
        self,
        *,
        where: dict[str, Any] | None = None,
        limit: int = 200,
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._store._list(
            self.spec, where=where, limit=limit, descending=descending
        )

    async def patch(self, key: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        """Read-modify-write a single record. Returns the updated record, or None if absent."""

        current = await self.get(key)
        if current is None:
            return None
        merged = {**current, **changes}
        await self._store._put(self.spec, merged)
        return merged

    async def count(self, *, where: dict[str, Any] | None = None) -> int:
        return await self._store._count(self.spec, where=where)


class DocStore:
    """Owns the connection (SQLite) or engine (PostgreSQL) shared by its tables."""

    def __init__(self, backend: str = "sqlite", url: str = "data/platform.db") -> None:
        self.backend = "postgres" if backend == "postgres" and url.startswith("postgres") else "sqlite"
        self.url = url
        self._conn: sqlite3.Connection | None = None
        self._engine: Any = None
        self._tables: dict[str, Any] = {}
        self._specs: dict[str, TableSpec] = {}
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------------

    async def start(self, specs: list[TableSpec]) -> None:
        for spec in specs:
            # `immutable` is enforced by omitting columns from the upsert's SET clause, which only
            # works for real columns — a name that lands in the JSON payload would be silently
            # overwritten on every put. Fail loudly at startup instead of losing data later.
            undeclared = [
                name for name in spec.immutable
                if name != spec.key and name not in {c.name for c in spec.columns}
            ]
            if undeclared:
                raise ValueError(
                    f"TableSpec {spec.name!r} lists immutable column(s) {undeclared} that are not "
                    "declared in `columns`; immutability cannot be enforced for payload keys"
                )
            self._specs[spec.name] = spec
        if self.backend == "postgres":
            await self._start_postgres(specs)
        else:
            await self._start_sqlite(specs)
        log.info("docstore ready (%s): %s", self.backend, ", ".join(s.name for s in specs))

    async def stop(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.close()
            self._conn = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def table(self, spec: TableSpec) -> Table:
        return Table(self, spec)

    async def _start_sqlite(self, specs: list[TableSpec]) -> None:
        Path(self.url).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.url, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            for spec in specs:
                cols = [f"{spec.key} TEXT PRIMARY KEY"]
                cols += [f"{c.name} {_SQLITE_TYPES[c.type]}" for c in spec.columns]
                cols.append("payload TEXT")
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {spec.name} ({', '.join(cols)})"  # noqa: S608
                )
                # tolerate a table created by an older build that lacks newer columns
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({spec.name})")}
                for c in (*spec.columns, Column("payload")):
                    if c.name not in have:
                        self._conn.execute(
                            f"ALTER TABLE {spec.name} ADD COLUMN {c.name} {_SQLITE_TYPES[c.type]}"
                        )
                for c in spec.columns:
                    if c.indexed:
                        self._conn.execute(
                            f"CREATE INDEX IF NOT EXISTS idx_{spec.name}_{c.name} "
                            f"ON {spec.name}({c.name})"
                        )
            self._conn.commit()

    async def _start_postgres(self, specs: list[TableSpec]) -> None:
        from sqlalchemy import Column as SAColumn
        from sqlalchemy import Float, Integer, MetaData, String, Text
        from sqlalchemy import Table as SATable
        from sqlalchemy.ext.asyncio import create_async_engine

        sa_types = {"text": Text, "real": Float, "int": Integer, "bool": Integer}
        self._engine = create_async_engine(self.url, pool_pre_ping=True)
        md = MetaData()
        for spec in specs:
            cols = [SAColumn(spec.key, String(96), primary_key=True)]
            cols += [
                SAColumn(c.name, sa_types[c.type], index=c.indexed) for c in spec.columns
            ]
            cols.append(SAColumn("payload", Text))
            self._tables[spec.name] = SATable(spec.name, md, *cols)
        async with self._engine.begin() as conn:
            await conn.run_sync(md.create_all)
            # add columns a previously-created table may be missing
            for spec in specs:
                from sqlalchemy import text as sa_text

                pg_types = {"text": "TEXT", "real": "DOUBLE PRECISION",
                            "int": "INTEGER", "bool": "INTEGER"}
                for c in spec.columns:
                    await conn.execute(sa_text(
                        f"ALTER TABLE {spec.name} ADD COLUMN IF NOT EXISTS "
                        f"{c.name} {pg_types[c.type]}"
                    ))

    # -- operations ------------------------------------------------------------

    async def _put(self, spec: TableSpec, record: dict[str, Any]) -> None:
        row = spec.split(record)
        if row.get(spec.key) is None:
            raise ValueError(f"{spec.name}: missing primary key {spec.key!r}")
        mutable = [c for c in spec.column_names
                   if c != spec.key and c not in spec.immutable]
        if self.backend == "postgres":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            t = self._tables[spec.name]
            stmt = pg_insert(t).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[spec.key], set_={c: row[c] for c in mutable}
            )
            async with self._engine.begin() as conn:
                await conn.execute(stmt)
            return
        assert self._conn is not None
        cols = spec.column_names
        sets = ", ".join(f"{c}=excluded.{c}" for c in mutable)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {spec.name} ({','.join(cols)}) "  # noqa: S608
                f"VALUES ({','.join('?' for _ in cols)}) "
                f"ON CONFLICT({spec.key}) DO UPDATE SET {sets}",
                tuple(row[c] for c in cols),
            )
            self._conn.commit()

    async def _get(self, spec: TableSpec, key: str) -> dict[str, Any] | None:
        if self.backend == "postgres":
            from sqlalchemy import select

            t = self._tables[spec.name]
            async with self._engine.connect() as conn:
                r = (await conn.execute(
                    select(t).where(t.c[spec.key] == key)
                )).mappings().first()
            return spec.merge(dict(r)) if r else None
        assert self._conn is not None
        with self._lock:
            r = self._conn.execute(
                f"SELECT * FROM {spec.name} WHERE {spec.key} = ?", (key,)  # noqa: S608
            ).fetchone()
        return spec.merge(dict(r)) if r else None

    async def _delete(self, spec: TableSpec, key: str) -> bool:
        if self.backend == "postgres":
            from sqlalchemy import delete

            t = self._tables[spec.name]
            async with self._engine.begin() as conn:
                res = await conn.execute(delete(t).where(t.c[spec.key] == key))
            return bool(res.rowcount)
        assert self._conn is not None
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM {spec.name} WHERE {spec.key} = ?", (key,)  # noqa: S608
            )
            self._conn.commit()
        return cur.rowcount > 0

    async def _list(
        self,
        spec: TableSpec,
        *,
        where: dict[str, Any] | None,
        limit: int,
        descending: bool,
    ) -> list[dict[str, Any]]:
        clause, params = _where_sql(spec, where)
        order = f"{spec.order_by} {'DESC' if descending else 'ASC'}"
        if self.backend == "postgres":
            from sqlalchemy import text as sa_text

            sql = (f"SELECT * FROM {spec.name}{clause} "  # noqa: S608
                   f"ORDER BY {order} LIMIT {int(limit)}")
            bound = {f"p{i}": v for i, v in enumerate(params)}
            for i in range(len(params)):
                sql = sql.replace("?", f":p{i}", 1)
            async with self._engine.connect() as conn:
                rows = (await conn.execute(sa_text(sql), bound)).mappings().all()
            return [spec.merge(dict(r)) for r in rows]
        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM {spec.name}{clause} ORDER BY {order} LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [spec.merge(dict(r)) for r in rows]

    async def _count(self, spec: TableSpec, *, where: dict[str, Any] | None) -> int:
        clause, params = _where_sql(spec, where)
        if self.backend == "postgres":
            from sqlalchemy import text as sa_text

            sql = f"SELECT COUNT(*) AS n FROM {spec.name}{clause}"  # noqa: S608
            bound = {f"p{i}": v for i, v in enumerate(params)}
            for i in range(len(params)):
                sql = sql.replace("?", f":p{i}", 1)
            async with self._engine.connect() as conn:
                r = (await conn.execute(sa_text(sql), bound)).mappings().first()
            return int(r["n"]) if r else 0
        assert self._conn is not None
        with self._lock:
            r = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {spec.name}{clause}", tuple(params)  # noqa: S608
            ).fetchone()
        return int(r["n"]) if r else 0
