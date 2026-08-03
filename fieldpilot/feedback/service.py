"""Store and query supervisor approve/reject decisions on alerts."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.feedback")

FEEDBACK_TABLE = TableSpec(
    "feedback",
    key="feedback_id",
    columns=(
        Column("alert_id", indexed=True),
        Column("event_id"),
        Column("event_type", indexed=True),
        Column("decision", indexed=True),      # approve | reject
        Column("label"),                       # corrected class label, if the reviewer gave one
        Column("image_path"),                  # frame on disk that produced the detection
        Column("zone", indexed=True),
        Column("worker_id"),
        Column("reviewer"),
        Column("notes"),
        Column("confidence", "real"),          # model confidence at detection time
        Column("consumed_at", "real", indexed=True),   # NULL until a training run claims it
        Column("consumed_by"),                 # run_id that claimed it
        Column("created_at", "real"),
    ),
)


class FeedbackDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


def _detected_class(payload: dict[str, Any]) -> str | None:
    """The detector's own class name for what was flagged, if the producer recorded one.

    `safety.ppe` stamps `meta["class"]` (e.g. "NO-Hardhat") and `inspection.detector` stamps
    `defect`; both survive into the event payload via `events.bridge`.
    """

    for key in ("class", "detected_class", "cls_name", "defect"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


class FeedbackService:
    """Persists review decisions and hands unconsumed ones to the learning loop."""

    def __init__(self, store: DocStore) -> None:
        self._table = store.table(FEEDBACK_TABLE)

    async def record(
        self,
        *,
        alert: dict[str, Any],
        decision: str,
        label: str | None = None,
        reviewer: str = "supervisor",
        notes: str = "",
        bbox: list[float] | None = None,
    ) -> dict[str, Any]:
        """Record one decision. `bbox` is [x1, y1, x2, y2] in pixels of `image_path`."""

        parsed = FeedbackDecision(str(decision).lower())
        payload = alert.get("payload") or {}
        row = {
            "feedback_id": uuid.uuid4().hex,
            "alert_id": alert.get("alert_id"),
            "event_id": payload.get("event_id") or alert.get("event_id"),
            "event_type": alert.get("event_type"),
            "decision": parsed.value,
            # The label must name a class the detector actually predicts ("NO-Hardhat"), which is
            # what `meta["class"]` carries through the bridge. Defaulting to `event_type` ("ppe")
            # would look fine here and then be silently dropped by the dataset builder, so an
            # unresolvable label is left None and skipped visibly instead.
            "label": label or _detected_class(payload),
            "image_path": alert.get("image_path") or payload.get("image_path"),
            "zone": alert.get("zone"),
            "worker_id": alert.get("worker_id"),
            "reviewer": reviewer,
            "notes": notes,
            "confidence": float(alert.get("confidence") or 0.0),
            "consumed_at": None,
            "consumed_by": None,
            "created_at": time.time(),
            # undeclared keys ride along in the payload column
            "bbox": bbox or payload.get("bbox"),
            "image_url": alert.get("image_url"),
            "alert_snapshot": {
                "severity": alert.get("severity"),
                "message": alert.get("message"),
                "hit_count": alert.get("hit_count"),
            },
        }
        stored = await self._table.put(row)
        log.info("feedback %s on alert %s by %s", parsed.value, row["alert_id"], reviewer)
        return stored

    async def list(
        self,
        *,
        decision: str | None = None,
        event_type: str | None = None,
        alert_id: str | None = None,
        unconsumed_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {}
        if decision:
            where["decision"] = decision
        if event_type:
            where["event_type"] = event_type
        if alert_id:
            where["alert_id"] = alert_id
        if unconsumed_only:
            where["consumed_at"] = ("isnull", True)
        return await self._table.list(where=where or None, limit=limit)

    async def for_alert(self, alert_id: str) -> dict[str, Any] | None:
        rows = await self._table.list(where={"alert_id": alert_id}, limit=1)
        return rows[0] if rows else None

    async def stats(self) -> dict[str, Any]:
        approved = await self._table.count(where={"decision": "approve"})
        rejected = await self._table.count(where={"decision": "reject"})
        pending = await self._table.count(where={"consumed_at": ("isnull", True)})
        total = approved + rejected
        return {
            "approved": approved,
            "rejected": rejected,
            "total": total,
            "unconsumed": pending,
            "approval_rate": round(approved / total, 4) if total else None,
        }

    async def claim_for_training(self, run_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Atomically-enough claim unconsumed rows for a run so samples aren't reused.

        Single-writer backend, so a read-then-stamp loop is sufficient; the `consumed_by`
        stamp is what makes a completed run's dataset reconstructable after the fact.
        """

        rows = await self.list(unconsumed_only=True, limit=limit)
        claimed = []
        now = time.time()
        for row in rows:
            updated = await self._table.patch(
                row["feedback_id"], {"consumed_at": now, "consumed_by": run_id}
            )
            claimed.append(updated or row)
        if claimed:
            log.info("run %s claimed %d feedback samples", run_id, len(claimed))
        return claimed

    async def release(self, run_id: str) -> int:
        """Un-claim a failed run's samples so they are available to the next attempt."""

        rows = await self._table.list(where={"consumed_by": run_id}, limit=10000)
        for row in rows:
            await self._table.patch(
                row["feedback_id"], {"consumed_at": None, "consumed_by": None}
            )
        return len(rows)
