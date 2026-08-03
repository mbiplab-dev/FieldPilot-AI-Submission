"""Ship queued events to the central API, retrying until they are acknowledged."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.offline.outbox import Outbox, as_epoch

log = get_logger("fieldpilot.offline.forwarder")


class StoreAndForward:
    """Enqueue-then-forward transport for edge events.

    `submit()` never raises on a network problem and never blocks the detection loop: the event is
    persisted first, then a background flusher drains the queue whenever the backend answers. With
    no `central_api` configured the outbox simply accumulates, which is the correct offline-only
    behaviour rather than an error.
    """

    def __init__(
        self,
        outbox: Outbox,
        *,
        central_api: str | None = None,
        endpoint: str = "/events",
        flush_interval_s: float = 5.0,
        batch_size: int = 100,
        timeout_s: float = 5.0,
    ) -> None:
        self.outbox = outbox
        self.central_api = central_api.rstrip("/") if central_api else None
        self.endpoint = endpoint
        self.flush_interval_s = flush_interval_s
        self.batch_size = batch_size
        self.timeout_s = timeout_s
        self.online: bool | None = None          # None = not yet probed
        self._flusher: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._running = False
        self.flushed_total = 0
        self._warned_no_httpx = False

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._flusher = asyncio.create_task(self._flush_loop(), name="offline-flusher")
        log.info("store-and-forward started (central_api=%s)", self.central_api or "none")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None

    # -- public ----------------------------------------------------------------

    async def submit(self, event: dict[str, Any]) -> None:
        """Durably record an event, then nudge the flusher. Never raises on transport errors."""

        await self.outbox.enqueue(event)
        self._wake.set()

    async def status(self) -> dict[str, Any]:
        counts = await self.outbox.counts()
        return {
            "central_api": self.central_api,
            "online": self.online,
            "flushed_total": self.flushed_total,
            **counts,
        }

    async def flush_once(self) -> dict[str, Any]:
        """Attempt one drain pass. Returns what happened, for tests and `/offline/flush`."""

        if not self.central_api:
            return {"sent": 0, "failed": 0, "reason": "no central_api configured"}
        rows = await self.outbox.pending(limit=self.batch_size)
        if not rows:
            self.online = True
            return {"sent": 0, "failed": 0}

        sent = failed = 0
        for row in rows:
            event = row.get("event") or {}
            ok, error = await self._post(event, row)
            if ok:
                await self.outbox.mark_sent(row["event_id"])
                sent += 1
                self.flushed_total += 1
            else:
                await self.outbox.mark_failed(
                    row["event_id"], error or "unknown", int(row.get("attempts") or 0) + 1
                )
                failed += 1
                break        # backend is down: stop hammering it, preserve ordering

        self.online = failed == 0
        if sent:
            log.info("flushed %d queued events (%d still pending)", sent, failed)
        return {"sent": sent, "failed": failed}

    # -- internals -------------------------------------------------------------

    async def _post(self, event: dict[str, Any], row: dict[str, Any]) -> tuple[bool, str | None]:
        """POST one event with reconciled timestamps. 2xx = acknowledged."""

        body = dict(event)
        # `timestamp` stays as recorded — that is when the hazard happened. The delivery
        # metadata tells the backend this arrived late, without rewriting history.
        enqueued_at = as_epoch(row.get("enqueued_at"), time.time())
        body["enqueued_at"] = enqueued_at
        body["forwarded_at"] = time.time()
        delay = float(body["forwarded_at"]) - enqueued_at
        body["offline_delay_s"] = round(max(0.0, delay), 3)
        body["replayed"] = delay > 1.0
        try:
            import httpx
        except ImportError:
            # Not an outage: the edge install lacks the HTTP client entirely. Retrying forever
            # would report as a normal network drop and hide a packaging problem.
            if not self._warned_no_httpx:
                log.error(
                    "httpx is not installed — queued events cannot be forwarded. Install the "
                    "`server` extra (uv sync --extra server). Events remain safely queued."
                )
                self._warned_no_httpx = True
            return False, "httpx not installed (edge install is missing the `server` extra)"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(f"{self.central_api}{self.endpoint}", json=body)
            if 200 <= resp.status_code < 300:
                return True, None
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001 — offline is the expected case, not an error
            return False, f"{type(exc).__name__}: {exc}"

    async def _flush_loop(self) -> None:
        backoff = self.flush_interval_s
        while self._running:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=backoff)
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._running:
                return
            try:
                result = await self.flush_once()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("flush pass failed")
                result = {"failed": 1}
            # back off while offline so a long outage is cheap; snap back when it clears
            if result.get("failed"):
                backoff = min(backoff * 2, 60.0)
            else:
                backoff = self.flush_interval_s
