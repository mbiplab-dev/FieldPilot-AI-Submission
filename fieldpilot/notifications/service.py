"""Notification service.

    Rules Engine action → NotificationService → channel senders

- **Deduplication:** the same subject on the same channel inside `dedup_window_s`
  collapses into one notification (a counter on the dedup key tracks merges).
- **Channels:** `dashboard` is built in (persisted + published on the bus). SMS / email /
  WhatsApp / push are pluggable async senders registered at runtime; without a sender
  configured the notification is persisted as `skipped` so nothing is silently lost.
- **Retry:** a failed send is re-queued with exponential backoff up to `max_attempts`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fieldpilot.events.bus import EventBus
from fieldpilot.logging_.logger import get_logger
from fieldpilot.triggers.cache import TriggerCache

log = get_logger("fieldpilot.notifications")

TOPIC_DASHBOARD = "notifications.dashboard"

Sender = Callable[[str, str, dict[str, Any]], Awaitable[bool]]  # (subject, body, meta) -> ok

SEVERITY_CHANNELS: dict[str, list[str]] = {
    "low": ["dashboard"],
    "medium": ["dashboard", "push"],
    "high": ["dashboard", "push", "email"],
    "critical": ["dashboard", "push", "sms", "whatsapp", "email"],
}


class NotificationService:
    def __init__(
        self,
        store,
        cache: TriggerCache,
        bus: EventBus | None = None,
        *,
        dedup_window_s: float = 300.0,
        max_attempts: int = 3,
        base_backoff_s: float = 2.0,
        clock=time.time,
    ) -> None:
        self.store = store
        self.cache = cache
        self.bus = bus
        self.dedup_window_s = dedup_window_s
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self.clock = clock
        self._senders: dict[str, Sender] = {}

    def register_sender(self, channel: str, sender: Sender) -> None:
        """Plug in a real channel integration (Twilio, SES, WhatsApp Business, FCM…)."""

        self._senders[channel] = sender

    async def notify(
        self,
        *,
        dedup_key: str,
        subject: str,
        body: str,
        severity: str = "medium",
        channels: list[str] | None = None,
        alert_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fan out one logical notification to its channels, deduplicated per channel."""

        targets = channels or SEVERITY_CHANNELS.get(severity, ["dashboard"])
        results = []
        for channel in targets:
            note = await self._notify_channel(
                channel, dedup_key=dedup_key, subject=subject, body=body,
                alert_id=alert_id, meta=meta or {},
            )
            if note is not None:
                results.append(note)
        return results

    async def _notify_channel(
        self, channel: str, *, dedup_key: str, subject: str, body: str,
        alert_id: str | None, meta: dict[str, Any],
    ) -> dict[str, Any] | None:
        now = self.clock()
        ckey = f"notif:{channel}:{dedup_key}"
        existing = await self.cache.get(ckey)
        if existing is not None:
            # duplicate inside the window — merge silently, count it.
            existing["merged"] = int(existing.get("merged", 0)) + 1
            await self.cache.set(ckey, existing, ttl_s=int(self.dedup_window_s))
            log.debug("notification deduped (%s/%s ×%d)", channel, dedup_key, existing["merged"])
            return None

        note = {
            "notification_id": uuid.uuid4().hex,
            "dedup_key": dedup_key,
            "channel": channel,
            "subject": subject,
            "body": body,
            "status": "queued",
            "attempts": 0,
            "alert_id": alert_id,
            "created_at": now,
            "sent_at": None,
        }
        await self.cache.set(ckey, {"merged": 0, "notification_id": note["notification_id"]},
                             ttl_s=int(self.dedup_window_s))
        await self._deliver(note, meta)
        return note

    async def _deliver(self, note: dict[str, Any], meta: dict[str, Any]) -> None:
        channel = note["channel"]
        if channel == "dashboard":
            note["status"] = "sent"
            note["sent_at"] = self.clock()
            if self.bus is not None:
                await self.bus.publish(TOPIC_DASHBOARD, {**note, "meta": meta})
            await self.store.save_notification(note)
            return

        sender = self._senders.get(channel)
        if sender is None:
            note["status"] = "skipped"  # no integration configured — persisted for audit
            await self.store.save_notification(note)
            return

        for attempt in range(1, self.max_attempts + 1):
            note["attempts"] = attempt
            try:
                ok = await sender(note["subject"], note["body"], meta)
            except Exception:  # noqa: BLE001 — a channel must never crash the pipeline
                log.exception("notification sender %s raised", channel)
                ok = False
            if ok:
                note["status"] = "sent"
                note["sent_at"] = self.clock()
                break
            if attempt < self.max_attempts:
                await asyncio.sleep(self.base_backoff_s * (2 ** (attempt - 1)))
        else:
            note["status"] = "failed"
        await self.store.save_notification(note)
