"""Offline store-and-forward for the edge device (PRD §4.5).

Wi-Fi on a construction site drops. When the backend is unreachable the edge appends every event
to a local SQLite outbox and keeps detecting; when connectivity returns the queue is flushed in
order. Two properties make the "zero dropped events" criterion real:

* **Nothing is enqueued optimistically.** An event is only marked sent after the backend
  acknowledges it, so a crash mid-flush replays rather than loses.
* **The flush is idempotent.** Events carry a stable `event_id` and the backend upserts on it, so
  a replayed batch cannot create duplicates.

Timestamps are reconciled on flush: the original `timestamp` is preserved (that is when the hazard
happened) and `enqueued_at`/`flushed_at` are attached so the backend can tell a delayed event from
a live one.
"""

from fieldpilot.offline.forwarder import StoreAndForward
from fieldpilot.offline.outbox import OUTBOX_TABLE, Outbox

__all__ = ["OUTBOX_TABLE", "Outbox", "StoreAndForward"]
