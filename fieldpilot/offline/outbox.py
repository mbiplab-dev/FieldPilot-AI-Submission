"""The durable local queue behind store-and-forward."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.offline.outbox")


def as_epoch(value: Any, default: float | None = None) -> float:
    """Coerce a timestamp to epoch seconds.

    Events reach the outbox both as live objects (float timestamps) and as JSON-serialised dicts
    from `Event.model_dump_json_safe()`, which renders the timestamp as an ISO-8601 string. The
    queue must accept either, since the edge's forwarding path uses the JSON form.
    """

    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            log.debug("unparseable timestamp %r — using default", value)
    if isinstance(value, datetime):
        return value.timestamp()
    return default if default is not None else time.time()

OUTBOX_TABLE = TableSpec(
    "event_outbox",
    key="event_id",
    columns=(
        Column("event_type", indexed=True),
        Column("status", indexed=True),      # pending | sent
        Column("attempts", "int"),
        Column("last_error"),
        Column("timestamp", "real"),         # when the hazard actually happened
        Column("enqueued_at", "real"),
        Column("flushed_at", "real"),
        Column("created_at", "real"),
    ),
    order_by="enqueued_at",
)


class Outbox:
    """Append-only-ish queue of events awaiting acknowledgement from the backend.

    Keyed by `event_id`, so enqueuing the same event twice (a retry of the *enqueue*, not of the
    send) collapses into one row instead of duplicating work.
    """

    def __init__(self, store: DocStore) -> None:
        self._table = store.table(OUTBOX_TABLE)

    async def enqueue(self, event: dict[str, Any]) -> dict[str, Any]:
        event_id = event.get("event_id")
        if not event_id:
            raise ValueError("event is missing event_id — cannot guarantee idempotent replay")
        existing = await self._table.get(str(event_id))
        if existing is not None and existing.get("status") == "sent":
            return existing          # already delivered; never resurrect it
        now = time.time()
        return await self._table.put({
            "event_id": str(event_id),
            "event_type": event.get("event_type"),
            "status": "pending",
            "attempts": int((existing or {}).get("attempts") or 0),
            "last_error": None,
            "timestamp": as_epoch(event.get("timestamp"), now),
            "enqueued_at": as_epoch((existing or {}).get("enqueued_at"), now),
            "flushed_at": None,
            "event": event,           # full payload rides in the JSON column
        })

    async def pending(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Oldest-first, so replay preserves the order the hazards occurred in."""

        return await self._table.list(
            where={"status": "pending"}, limit=limit, descending=False
        )

    async def mark_sent(self, event_id: str) -> None:
        await self._table.patch(event_id, {"status": "sent", "flushed_at": time.time()})

    async def mark_failed(self, event_id: str, error: str, attempts: int) -> None:
        await self._table.patch(
            event_id, {"attempts": attempts, "last_error": error[:500]}
        )

    async def counts(self) -> dict[str, int]:
        return {
            "pending": await self._table.count(where={"status": "pending"}),
            "sent": await self._table.count(where={"status": "sent"}),
        }

    async def purge_sent(self, *, older_than_s: float = 86_400) -> int:
        """Drop acknowledged rows past their retention window."""

        cutoff = time.time() - older_than_s
        rows = await self._table.list(where={"status": "sent"}, limit=10_000)
        removed = 0
        for row in rows:
            if float(row.get("flushed_at") or 0) < cutoff:
                await self._table.delete(row["event_id"])
                removed += 1
        if removed:
            log.info("purged %d acknowledged outbox rows", removed)
        return removed
