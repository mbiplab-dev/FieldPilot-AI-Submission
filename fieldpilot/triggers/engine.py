"""Intelligent Trigger Engine.

Sits directly on the event bus between the models and everything else:

    Model → Event → [TRIGGER ENGINE] → Rules Engine → Notification → Dashboard

Responsibilities (per platform spec):

- **Ignore duplicate alerts.** Same helmet violation within 45 s → occurrence SUPPRESSED.
- **Merge repeated detections.** One alert per underlying issue; `hit_count` tracks repeats.
- **Track active alerts.** NEW → ACTIVE while the issue persists.
- **Auto-resolve.** No detection for `resolve_after_s` → RESOLVED (+ `alerts.resolved`).
- **Operator suppression.** SUPPRESSED alerts merge silently until cleared.

The engine NEVER notifies anyone. It emits alerts onto the bus (`alerts.new`,
`alerts.resolved`, `alerts.updated`); the rules engine / notification service decide
what to do with them.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from fieldpilot.events.bus import EventBus
from fieldpilot.events.schema import Event
from fieldpilot.logging_.logger import get_logger
from fieldpilot.triggers.cache import TriggerCache

log = get_logger("fieldpilot.triggers.engine")

TOPIC_ALERT_NEW = "alerts.new"
TOPIC_ALERT_UPDATED = "alerts.updated"
TOPIC_ALERT_RESOLVED = "alerts.resolved"


class AlertState(StrEnum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"


@dataclass
class Alert:
    """One tracked, deduplicated issue."""

    alert_id: str
    dedup_key: str
    event_type: str
    worker_id: str | None
    camera_id: str
    zone: str | None
    severity: str
    state: str = AlertState.NEW
    hit_count: int = 1
    confidence: float = 0.0
    first_seen: float = 0.0
    last_seen: float = 0.0
    resolved_at: float | None = None
    suppressed_at: float | None = None
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    image_url: str | None = None
    video_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Alert:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ProcessResult:
    """Outcome of feeding one event through the engine."""

    outcome: str            # created | merged | reactivated | suppressed_duplicate | suppressed
    alert: Alert | None
    notified: bool = False  # True only when a NEW alert was emitted onto the bus


class TriggerEngine:
    def __init__(
        self,
        cache: TriggerCache,
        bus: EventBus | None = None,
        *,
        dedup_window_s: float = 45.0,
        resolve_after_s: float = 90.0,
        cache_ttl_s: int = 6 * 3600,
        alert_sink=None,  # async callable(alert_dict) — durable persistence hook
        clock=time.time,
    ) -> None:
        self.cache = cache
        self.bus = bus
        self.dedup_window_s = float(dedup_window_s)
        self.resolve_after_s = float(resolve_after_s)
        self.cache_ttl_s = int(cache_ttl_s)
        self.alert_sink = alert_sink
        self.clock = clock
        self._sweeper: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

    def start_sweeper(self, interval_s: float = 5.0) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop(interval_s), name="trigger-sweeper")

    async def stop(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass

    # -- core ----------------------------------------------------------------

    def _cache_key(self, dedup_key: str) -> str:
        return f"alert:{dedup_key}"

    async def process(self, event: Event) -> ProcessResult:
        """Feed one model event. Returns what happened; publishes alert topics on change."""

        now = self.clock()
        key = self._cache_key(event.dedup_key())
        cached = await self.cache.get(key)

        if cached is None:
            alert = Alert(
                alert_id=uuid.uuid4().hex,
                dedup_key=event.dedup_key(),
                event_type=event.event_type.value,
                worker_id=event.worker_id,
                camera_id=event.camera_id,
                zone=event.zone,
                severity=event.severity.value,
                state=AlertState.NEW,
                hit_count=1,
                confidence=event.confidence,
                first_seen=now,
                last_seen=now,
                message=str(event.payload.get("message") or f"{event.event_type.value} detected"),
                payload=dict(event.payload),
                image_url=event.image_url,
                video_url=event.video_url,
            )
            await self._save(key, alert)
            await self._emit(TOPIC_ALERT_NEW, alert)
            return ProcessResult(outcome="created", alert=alert, notified=True)

        alert = Alert.from_dict(cached)

        if alert.state == AlertState.SUPPRESSED:
            self._merge(alert, event, now)
            await self._save(key, alert)
            return ProcessResult(outcome="suppressed", alert=alert)

        if alert.state == AlertState.RESOLVED:
            # issue came back after auto-resolution → reactivate and re-notify.
            alert.state = AlertState.ACTIVE
            alert.resolved_at = None
            self._merge(alert, event, now)
            await self._save(key, alert)
            await self._emit(TOPIC_ALERT_NEW, alert)
            return ProcessResult(outcome="reactivated", alert=alert, notified=True)

        # state NEW or ACTIVE
        gap = now - alert.last_seen
        self._merge(alert, event, now)
        if gap < self.dedup_window_s:
            await self._save(key, alert)
            return ProcessResult(outcome="suppressed_duplicate", alert=alert)

        # repeated detection outside the dedup window → confirmed ongoing issue.
        if alert.state == AlertState.NEW:
            alert.state = AlertState.ACTIVE
        await self._save(key, alert)
        await self._emit(TOPIC_ALERT_UPDATED, alert)
        return ProcessResult(outcome="merged", alert=alert)

    def _merge(self, alert: Alert, event: Event, now: float) -> None:
        alert.hit_count += 1
        alert.last_seen = now
        alert.confidence = max(alert.confidence, event.confidence)
        if _severity_rank(event.severity.value) > _severity_rank(alert.severity):
            alert.severity = event.severity.value
        if event.image_url:
            alert.image_url = event.image_url
        if event.video_url:
            alert.video_url = event.video_url
        alert.payload.update({k: v for k, v in event.payload.items() if v is not None})

    async def _save(self, cache_key: str, alert: Alert) -> None:
        await self.cache.set(cache_key, alert.to_dict(), self.cache_ttl_s)
        if self.alert_sink is not None:
            await self.alert_sink(alert.to_dict())

    async def _emit(self, topic: str, alert: Alert) -> None:
        if self.bus is not None:
            await self.bus.publish(topic, alert.to_dict())

    # -- operator actions ------------------------------------------------------

    async def resolve(self, dedup_key: str, *, auto: bool = False) -> Alert | None:
        key = self._cache_key(dedup_key)
        cached = await self.cache.get(key)
        if cached is None:
            return None
        alert = Alert.from_dict(cached)
        if alert.state in (AlertState.RESOLVED,):
            return alert
        alert.state = AlertState.RESOLVED
        alert.resolved_at = self.clock()
        await self._save(key, alert)
        await self._emit(TOPIC_ALERT_RESOLVED, alert)
        log.info("alert %s resolved (%s)", alert.alert_id, "auto" if auto else "manual")
        return alert

    async def suppress(self, dedup_key: str) -> Alert | None:
        key = self._cache_key(dedup_key)
        cached = await self.cache.get(key)
        if cached is None:
            return None
        alert = Alert.from_dict(cached)
        alert.state = AlertState.SUPPRESSED
        alert.suppressed_at = self.clock()
        await self._save(key, alert)
        await self._emit(TOPIC_ALERT_UPDATED, alert)
        return alert

    async def unsuppress(self, dedup_key: str) -> Alert | None:
        key = self._cache_key(dedup_key)
        cached = await self.cache.get(key)
        if cached is None:
            return None
        alert = Alert.from_dict(cached)
        if alert.state == AlertState.SUPPRESSED:
            alert.state = AlertState.ACTIVE
            alert.suppressed_at = None
            await self._save(key, alert)
            await self._emit(TOPIC_ALERT_UPDATED, alert)
        return alert

    async def get(self, dedup_key: str) -> Alert | None:
        cached = await self.cache.get(self._cache_key(dedup_key))
        return Alert.from_dict(cached) if cached else None

    async def set_verdict(self, dedup_key: str, verdict: dict) -> Alert | None:
        """Stamp an LLM verdict onto the alert (payload.llm_verdict) + persist + emit."""

        key = self._cache_key(dedup_key)
        cached = await self.cache.get(key)
        if cached is None:
            return None
        alert = Alert.from_dict(cached)
        alert.payload["llm_verdict"] = verdict
        await self._save(key, alert)
        await self._emit(TOPIC_ALERT_UPDATED, alert)
        return alert

    async def list_tracked(self) -> list[Alert]:
        items = await self.cache.scan("alert:*")
        return [Alert.from_dict(v) for _, v in items]

    # -- auto-resolution -------------------------------------------------------

    async def sweep_once(self) -> list[Alert]:
        """Resolve alerts whose issue has disappeared. Called by the sweeper loop."""

        now = self.clock()
        resolved: list[Alert] = []
        for _, raw in await self.cache.scan("alert:*"):
            alert = Alert.from_dict(raw)
            if alert.state not in (AlertState.NEW, AlertState.ACTIVE):
                continue
            if now - alert.last_seen >= self.resolve_after_s:
                out = await self.resolve(alert.dedup_key, auto=True)
                if out is not None:
                    resolved.append(out)
        return resolved

    async def _sweep_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001 — sweeper must never die
                log.exception("trigger sweeper iteration failed")


def _severity_rank(sev: str) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(sev, 0)
