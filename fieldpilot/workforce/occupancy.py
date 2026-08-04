"""Zone occupancy: who is checked in where, and which zones are generating the warnings.

A worker checks in to a zone and later checks out. Two things fall out of that record:

  * **presence** — the site manager can see who is where right now, which is what makes a
    zone-scoped advisory ("everyone in zone-b, edge protection is down") addressable to people
    rather than to cameras;
  * **exposure** — a zone with five workers in it and four open warnings is a different problem
    from an empty zone with the same four warnings. `occupancy_report` combines the two into one
    ranked table.

Invariant: **a worker is in at most one zone at a time.** `enter` enforces it by closing any
occupancy still open elsewhere, so a missed check-out cannot leave a worker apparently present in
two places. The auto-closed record is returned to the caller so it can be reported rather than
happening silently.

This module deliberately has **no event-bus dependency**. Check-ins are not safety detections and
must not enter the `Model -> Event -> Trigger -> Rules -> Notification` chain — that chain exists
for hazards, and filling it with attendance traffic would dilute it. The service returns records;
the caller (the API layer) publishes whatever lightweight bus message it wants. Keeping the bus
out also keeps this unit-testable with nothing but a SQLite file.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.workforce.occupancy")

OCCUPANCY_TABLE = TableSpec(
    "zone_occupancy",
    key="occupancy_id",
    columns=(
        Column("worker_id", indexed=True),
        Column("zone_id", indexed=True),
        Column("entered_at", "real"),
        Column("left_at", "real"),          # NULL while the worker is still in the zone
        Column("created_at", "real"),
    ),
)

#: an alert in one of these states is still someone's problem
OUTSTANDING_STATES = ("NEW", "ACTIVE")

# --- risk model -------------------------------------------------------------------------------
# risk_raw = severity_load * worker_exposure * zone_hazard
#
#   severity_load   sum over the zone's alerts of SEVERITY_WEIGHTS[severity], with an
#                   OUTSTANDING_MULTIPLIER applied to alerts still NEW/ACTIVE — an unresolved
#                   warning is worth more than one somebody has already dealt with.
#   worker_exposure 1 + WORKER_EXPOSURE * workers_present. Starts at 1 rather than 0 so a
#                   dangerous but momentarily empty zone does not score zero and disappear.
#   zone_hazard     HAZARD_MULTIPLIERS[hazard_level], times DANGER_MULTIPLIER for a danger zone.
#
# risk_score is risk_raw rescaled so the worst zone in the report is 100.0 and a zone with no
# alerts is 0.0. It is a *comparison* between the zones handed in, not an absolute index — which
# is exactly the question the UI asks ("which zone has more warnings"), and it keeps the number
# explainable: 100 means "worst right now", 50 means "half as bad as the worst".
#
# Everything here is a fixed constant and a sum: the same inputs always produce the same table.
SEVERITY_WEIGHTS: dict[str, float] = {"low": 1.0, "medium": 2.0, "high": 4.0, "critical": 8.0}
HAZARD_MULTIPLIERS: dict[str, float] = {"low": 0.8, "medium": 1.0, "high": 1.3}
OUTSTANDING_MULTIPLIER = 2.0
WORKER_EXPOSURE = 0.5
DANGER_MULTIPLIER = 1.15
DAY_S = 86400.0


class OccupancyMismatchError(ValueError):
    """Raised when a check-out names a zone the worker is not actually checked in to.

    A `ValueError` subclass so a caller that already maps `ValueError` to HTTP 400 keeps working,
    while one that wants to say "you are in zone-a, not zone-b" can catch this specifically.
    """


def _require(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _entered(row: Mapping[str, Any]) -> float:
    return float(row.get("entered_at") or 0.0)


class OccupancyService:
    """Check-in / check-out records over a `DocStore`, plus the manager's occupancy report."""

    def __init__(self, store: DocStore) -> None:
        self._table = store.table(OCCUPANCY_TABLE)

    async def start(self) -> None:
        """Nothing to seed; log the standing state so a restart is legible in the log."""

        open_now = await self._table.list(where={"left_at": ("isnull", True)}, limit=1000)
        log.info("occupancy ready — %d worker(s) currently checked in", len(open_now))

    # -- writes ----------------------------------------------------------------

    async def enter(self, worker_id: str, zone_id: str) -> dict[str, Any]:
        """Check `worker_id` in to `zone_id`.

        Returns `{"worker_id", "zone_id", "occupancy", "closed", "created"}` where:
          * `occupancy` is the open record for this worker in this zone;
          * `closed` is the occupancy auto-closed in the worker's previous zone, or None;
          * `created` is False when the worker was already in this zone (idempotent re-entry).

        The zone is not validated against the zone registry here — that stays the caller's job,
        so this module does not depend on `ZoneService`.
        """

        worker_id = _require(worker_id, "worker_id")
        zone_id = _require(zone_id, "zone_id")
        now = time.time()

        open_rows = await self._open_rows(worker_id)
        here = sorted((r for r in open_rows if r.get("zone_id") == zone_id), key=_entered)
        elsewhere = sorted(
            (r for r in open_rows if r.get("zone_id") != zone_id), key=_entered, reverse=True
        )

        # one worker, one zone: close whatever is still open somewhere else first
        closed = [await self._close(row, now) for row in elsewhere]
        if len(closed) > 1:
            log.warning(
                "worker %s had %d open occupancies in other zones — all closed", worker_id,
                len(closed),
            )

        if here:
            # Already here. Keep the EARLIEST open row so `entered_at` stays the moment the
            # worker actually arrived, and retire any duplicate rather than reporting it as a
            # zone change (it is the same zone — saying "left zone-a, entered zone-a" is worse
            # than saying nothing).
            current, duplicates = here[0], here[1:]
            for row in duplicates:
                await self._close(row, now)
            if duplicates:
                log.warning(
                    "worker %s had %d duplicate open occupancies in %s — retired",
                    worker_id, len(duplicates), zone_id,
                )
            return {
                "worker_id": worker_id,
                "zone_id": zone_id,
                "occupancy": current,
                "closed": closed[0] if closed else None,
                "created": False,
            }

        record = await self._table.put({
            "occupancy_id": uuid.uuid4().hex,
            "worker_id": worker_id,
            "zone_id": zone_id,
            "entered_at": now,
            "left_at": None,
            "created_at": now,
        })
        log.info("worker %s entered %s", worker_id, zone_id)
        return {
            "worker_id": worker_id,
            "zone_id": zone_id,
            "occupancy": record,
            "closed": closed[0] if closed else None,
            "created": True,
        }

    async def leave(self, worker_id: str, zone_id: str | None = None) -> dict[str, Any] | None:
        """Check `worker_id` out. Returns the closed record (with `duration_s`), or None if the
        worker was not checked in anywhere.

        Passing `zone_id` asserts which zone the worker believes they are leaving; a mismatch
        raises `OccupancyMismatchError` and changes nothing, so the caller can tell the worker
        where the system actually thinks they are instead of silently closing the wrong record.
        """

        worker_id = _require(worker_id, "worker_id")
        open_rows = sorted(await self._open_rows(worker_id), key=_entered, reverse=True)
        if not open_rows:
            return None
        current = open_rows[0]
        if zone_id and str(zone_id).strip() != current.get("zone_id"):
            raise OccupancyMismatchError(
                f"worker {worker_id!r} is checked in to {current.get('zone_id')!r}, "
                f"not {str(zone_id).strip()!r}"
            )
        now = time.time()
        closed = await self._close(current, now)
        for stray in open_rows[1:]:
            await self._close(stray, now)
        log.info(
            "worker %s left %s after %.1fs", worker_id, closed.get("zone_id"),
            closed.get("duration_s") or 0.0,
        )
        return closed

    async def _close(self, row: Mapping[str, Any], now: float) -> dict[str, Any]:
        entered = _entered(row) or now
        left = max(now, entered)          # never record a negative stay
        updated = await self._table.patch(
            str(row["occupancy_id"]),
            {"left_at": left, "duration_s": round(left - entered, 3)},
        )
        return updated if updated is not None else dict(row)

    # -- reads -----------------------------------------------------------------

    async def current_zone(self, worker_id: str) -> dict[str, Any] | None:
        """The worker's open occupancy record — it carries `zone_id` and `entered_at` — or None."""

        rows = sorted(await self._open_rows(str(worker_id or "")), key=_entered, reverse=True)
        return rows[0] if rows else None

    async def present_workers(self, zone_id: str | None = None) -> list[dict[str, Any]]:
        """Open occupancies, site-wide or for one zone, oldest arrival first."""

        where: dict[str, Any] = {"left_at": ("isnull", True)}
        if zone_id:
            where["zone_id"] = zone_id
        rows = await self._table.list(where=where, limit=1000)
        return sorted(rows, key=_entered)

    async def history(
        self,
        worker_id: str | None = None,
        zone_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Occupancies (open and closed) newest first, optionally filtered."""

        where: dict[str, Any] = {}
        if worker_id:
            where["worker_id"] = worker_id
        if zone_id:
            where["zone_id"] = zone_id
        return await self._table.list(where=where or None, limit=limit)

    async def _open_rows(self, worker_id: str) -> list[dict[str, Any]]:
        return await self._table.list(
            where={"worker_id": worker_id, "left_at": ("isnull", True)}, limit=100
        )

    # -- the manager view ------------------------------------------------------

    async def occupancy_report(
        self,
        zones: Iterable[Mapping[str, Any]],
        alerts: Iterable[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """One row per zone: who is in it, how many warnings it has, and how it ranks.

        `zones` is `ZoneService.list()` output and `alerts` is `PlatformStore.list_alerts()`
        output — this service reads both rather than importing either, so it stays decoupled from
        the zone registry and the platform store.

        Every zone handed in appears exactly once, including zones with no alerts and no workers
        (zeros, never omitted — a quiet zone is information). Rows come back worst-first;
        `risk_rank` is 1-based over that order. Ties break on total warnings, then `zone_id`, so
        the ordering is total and reproducible.

        `now` is injectable purely so the "today" window is deterministic in tests.
        """

        now = time.time() if now is None else float(now)
        day_start = now - DAY_S

        present: dict[str, list[dict[str, Any]]] = {}
        for row in await self.present_workers():
            present.setdefault(str(row.get("zone_id")), []).append(row)

        by_zone: dict[str, list[Mapping[str, Any]]] = {}
        unassigned = 0
        for alert in alerts:
            zone = alert.get("zone")
            if not zone:
                unassigned += 1
                continue
            by_zone.setdefault(str(zone), []).append(alert)
        if unassigned:
            log.debug("occupancy_report: %d alert(s) carry no zone and are not attributed",
                      unassigned)

        rows: list[dict[str, Any]] = []
        for zone in zones:
            zone_id = str(zone.get("zone_id"))
            hazard_level = str(zone.get("hazard_level") or "medium").lower()
            danger = bool(zone.get("danger"))
            workers = [
                {
                    "worker_id": r.get("worker_id"),
                    "occupancy_id": r.get("occupancy_id"),
                    "entered_at": r.get("entered_at"),
                    "duration_s": round(now - (_entered(r) or now), 1),
                }
                for r in present.get(zone_id, [])
            ]

            by_severity = dict.fromkeys(SEVERITY_WEIGHTS, 0)
            load = 0.0
            today = 0
            outstanding = 0
            zone_alerts = by_zone.get(zone_id, [])
            for alert in zone_alerts:
                severity = str(alert.get("severity") or "medium").lower()
                by_severity[severity] = by_severity.get(severity, 0) + 1
                still_open = str(alert.get("state") or "").upper() in OUTSTANDING_STATES
                outstanding += int(still_open)
                today += int(float(alert.get("first_seen") or 0.0) >= day_start)
                weight = SEVERITY_WEIGHTS.get(severity, SEVERITY_WEIGHTS["low"])
                load += weight * (OUTSTANDING_MULTIPLIER if still_open else 1.0)

            exposure = 1.0 + WORKER_EXPOSURE * len(workers)
            hazard = HAZARD_MULTIPLIERS.get(hazard_level, 1.0) * (
                DANGER_MULTIPLIER if danger else 1.0
            )
            rows.append({
                "zone_id": zone_id,
                "name": zone.get("name") or zone_id,
                "hazard_level": hazard_level,
                "danger": danger,
                "workers": workers,
                "worker_count": len(workers),
                "warnings": {
                    "total": len(zone_alerts),
                    "today": today,
                    "outstanding": outstanding,
                    "by_severity": by_severity,
                },
                "risk_raw": round(load * exposure * hazard, 4),
            })

        peak = max((r["risk_raw"] for r in rows), default=0.0)
        for row in rows:
            row["risk_score"] = round(100.0 * row["risk_raw"] / peak, 1) if peak > 0 else 0.0
        rows.sort(key=lambda r: (-r["risk_raw"], -r["warnings"]["total"], r["zone_id"]))
        for rank, row in enumerate(rows, start=1):
            row["risk_rank"] = rank
        return rows
