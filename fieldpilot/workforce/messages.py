"""Direct messages between a site manager and one worker.

Distinct from `questions.py` on purpose. A *question* is a formal request answered first by the LLM
and then authoritatively by a manager; it has a lifecycle and belongs on a review queue. A
*message* is a conversation — quick back-and-forth, no LLM in the loop, no status to resolve.
Conflating them would put "on my way" on the same queue as an unanswered compliance question.

Each worker has exactly one thread with the management side. There is no worker-to-worker channel:
this is a safety tool, and a private side-channel between workers is not something a supervisory
system should be building.

Voice notes are first-class. A worker wearing gloves on a loud site can hold a button far more
easily than they can type, so a message carries either text, an audio clip, or both.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.workforce.messages")

MESSAGES_TABLE = TableSpec(
    "worker_messages",
    key="message_id",
    columns=(
        # The worker whose thread this is — set for both directions, so one indexed lookup
        # retrieves the whole conversation regardless of who sent each message.
        Column("worker_id", indexed=True),
        Column("sender_role", indexed=True),   # worker | site_manager
        Column("sender_id"),
        Column("sender_name"),
        Column("text"),
        Column("audio_path"),
        Column("audio_seconds", "real"),
        Column("read_at", "real"),
        Column("created_at", "real"),
    ),
)

#: Roles allowed to send. Anything else is a programming error, not user input.
SENDER_ROLES = ("worker", "site_manager")


class MessageError(RuntimeError):
    """A message could not be accepted (empty, or from an unknown role)."""


class MessageService:
    """Persistence and retrieval for manager↔worker threads."""

    def __init__(self, store: DocStore) -> None:
        self._table = store.table(MESSAGES_TABLE)

    async def send(
        self,
        *,
        worker_id: str,
        sender_role: str,
        sender_id: str,
        sender_name: str | None = None,
        text: str = "",
        audio_path: str | None = None,
        audio_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Append one message to `worker_id`'s thread."""

        if sender_role not in SENDER_ROLES:
            raise MessageError(f"unknown sender role {sender_role!r}")
        body = (text or "").strip()
        if not body and not audio_path:
            # An empty message is a UI bug, not a thing to persist.
            raise MessageError("a message needs text or a voice note")

        record = {
            "message_id": uuid.uuid4().hex,
            "worker_id": worker_id,
            "sender_role": sender_role,
            "sender_id": sender_id,
            "sender_name": sender_name or sender_id,
            "text": body[:2000],
            "audio_path": audio_path,
            "audio_seconds": float(audio_seconds) if audio_seconds else None,
            "read_at": None,
            "created_at": time.time(),
        }
        await self._table.put(record)
        log.info("message %s -> thread %s from %s", record["message_id"], worker_id, sender_role)
        return record

    async def thread(self, worker_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """One worker's conversation, oldest first — the order a chat is read in."""

        rows = await self._table.list(where={"worker_id": worker_id}, limit=limit)
        return sorted(rows, key=lambda r: r.get("created_at") or 0)

    async def threads(self) -> list[dict[str, Any]]:
        """Every conversation, most recently active first — the manager's inbox.

        Includes `unread`, counting only messages *from the worker*: a manager does not need
        their own replies counted back at them.
        """

        rows = await self._table.list(limit=5000)
        by_worker: dict[str, dict[str, Any]] = {}
        for row in rows:
            worker_id = row.get("worker_id")
            if not worker_id:
                continue
            created = row.get("created_at") or 0
            entry = by_worker.setdefault(worker_id, {
                "worker_id": worker_id,
                "messages": 0,
                "unread": 0,
                "last_at": 0.0,
                "last_text": "",
                "last_sender_role": None,
                "last_has_audio": False,
                "worker_name": None,
            })
            entry["messages"] += 1
            if row.get("sender_role") == "worker":
                if not row.get("read_at"):
                    entry["unread"] += 1
                entry["worker_name"] = row.get("sender_name") or entry["worker_name"]
            if created >= entry["last_at"]:
                entry["last_at"] = created
                entry["last_text"] = row.get("text") or ""
                entry["last_sender_role"] = row.get("sender_role")
                entry["last_has_audio"] = bool(row.get("audio_path"))
        return sorted(by_worker.values(), key=lambda t: t["last_at"], reverse=True)

    async def mark_read(self, worker_id: str, *, reader_role: str) -> int:
        """Mark the *other* side's messages in this thread as read. Returns how many changed."""

        other = "worker" if reader_role == "site_manager" else "site_manager"
        rows = await self._table.list(where={"worker_id": worker_id}, limit=5000)
        now = time.time()
        changed = 0
        for row in rows:
            if row.get("sender_role") == other and not row.get("read_at"):
                await self._table.patch(row["message_id"], {"read_at": now})
                changed += 1
        return changed

    async def unread_for_manager(self) -> int:
        """Total unread worker messages across every thread, for the dashboard badge."""

        return sum(t["unread"] for t in await self.threads())


def resolve_audio_path(uploads_dir: str, stored: str | None) -> Path | None:
    """Map a stored audio filename to a real path, refusing anything outside the uploads dir.

    Mirrors `questions.resolve_image_path`: the stored name is generated server-side, but this is
    the boundary a traversal attempt would have to cross, so it is checked rather than trusted.
    """

    if not stored:
        return None
    root = Path(uploads_dir).resolve()
    candidate = (root / Path(stored).name).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate
