"""Event Bus — the only way AI models talk to the rest of the platform.

Models `publish(event)`; consumers `subscribe(topic, handler)`. Topics are dotted:

    events.all            every event
    events.ppe            one family
    alerts.new            emitted by the trigger engine
    alerts.resolved       emitted by the trigger engine

Two backends with identical semantics:

- `InMemoryEventBus` — asyncio queues; used in dev/tests and as an offline fallback.
- `RedisEventBus`    — redis pub/sub; used in production (multiple service replicas).

The backend is selected by `events.backend` in config.yaml. Redis is imported lazily so
the codebase (and its tests) runs with zero infrastructure.
"""

from __future__ import annotations

import asyncio
import collections
import fnmatch
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fieldpilot.events.schema import Event
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.events.bus")

TOPIC_ALL = "events.all"
ALERTS_NEW = "alerts.new"
ALERTS_RESOLVED = "alerts.resolved"

Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


def event_topic(event_type: str) -> str:
    return f"events.{event_type}"


class EventBus(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, topic: str, message: dict[str, Any]) -> None: ...
    async def subscribe(self, pattern: str, handler: Handler) -> None: ...


class InMemoryEventBus:
    """Single-process pub/sub over asyncio queues. Pattern subscribe via fnmatch."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=10_000)
        self._dispatcher: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="eventbus-dispatch")

    async def stop(self) -> None:
        self._running = False
        if self._dispatcher:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        if not self._running:
            return
        try:
            self._queue.put_nowait((topic, message))
        except asyncio.QueueFull:
            log.warning("event bus queue full — dropping message on %s", topic)

    async def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                topic, message = await self._queue.get()
            except asyncio.CancelledError:
                raise
            for pattern, handler in list(self._subs):
                if fnmatch.fnmatchcase(topic, pattern):
                    self._tasks.append(asyncio.create_task(self._safe(handler, topic, message)))

    @staticmethod
    async def _safe(handler: Handler, topic: str, message: dict[str, Any]) -> None:
        try:
            await handler(topic, message)
        except Exception:  # noqa: BLE001 — one bad consumer must not kill the bus
            log.exception("event handler failed for topic %s", topic)


class RedisEventBus:
    """Redis pub/sub backend. Lazy-imports redis.asyncio; identical interface to in-memory."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._redis = None
        self._pubsub = None
        self._subs: list[tuple[str, Handler]] = []
        self._pending_subs: collections.deque[str] = collections.deque()
        self._listener: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        import redis.asyncio as aioredis  # lazy: optional dependency

        self._redis = aioredis.from_url(self.url, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        # subscribe ONLY registered patterns: Redis delivers one pmessage per matching
        # pattern, so overlapping defaults (events.* + alerts.*) would double-dispatch.
        patterns = sorted({p for p, _ in self._subs})
        if patterns:
            await self._pubsub.psubscribe(*patterns)
        self._running = True
        self._listener = asyncio.create_task(self._listen_loop(), name="eventbus-redis-listen")

    async def stop(self) -> None:
        self._running = False
        if self._listener:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.close()
        if self._redis:
            await self._redis.aclose()

    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        if not self._running or self._redis is None:
            return
        await self._redis.publish(topic, json.dumps(message, default=str))

    async def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))
        # redis-py PubSub is NOT coroutine-safe: psubscribe must happen inside the listener
        # task, never concurrently from here — queue it and let the loop apply it.
        self._pending_subs.append(pattern)

    async def _listen_loop(self) -> None:
        assert self._pubsub is not None
        # Redis delivers one pmessage per matching pattern; with overlapping patterns the
        # SAME (channel, data) arrives twice — dedupe recent deliveries so handlers fire once.
        recent: collections.deque[tuple[str, str]] = collections.deque(maxlen=512)
        while self._running:
            while self._pending_subs:
                pattern = self._pending_subs.popleft()
                try:
                    await self._pubsub.psubscribe(pattern)
                except Exception:  # noqa: BLE001
                    log.exception("psubscribe failed for pattern %s", pattern)
            if not self._subs:
                # publish-only client: get_message would raise with zero subscriptions
                await asyncio.sleep(0.2)
                continue
            try:
                msg = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0)
            except Exception:  # noqa: BLE001 — transient redis errors must not kill the loop
                log.exception("redis pubsub error — retrying")
                await asyncio.sleep(1.0)
                continue
            if msg is None:
                continue
            topic = msg.get("channel") or ""
            raw = msg.get("data") or "{}"
            key = (topic, raw)
            if key in recent:
                continue
            recent.append(key)
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            fired: set[int] = set()
            for pattern, handler in list(self._subs):
                if fnmatch.fnmatchcase(topic, pattern) and id(handler) not in fired:
                    fired.add(id(handler))
                    asyncio.create_task(InMemoryEventBus._safe(handler, topic, payload))


def publish_event(bus: EventBus, event: Event) -> Awaitable[None]:
    """Publish an event to both its family topic and the global topic."""

    async def _pub() -> None:
        msg = event.model_dump_json_safe()
        await bus.publish(event_topic(event.event_type.value), msg)
        await bus.publish(TOPIC_ALL, msg)

    return _pub()


def create_bus(backend: str = "memory", redis_url: str = "redis://localhost:6379/0") -> EventBus:
    if backend == "redis":
        try:
            import redis.asyncio  # noqa: F401
        except ImportError:
            log.warning("redis package unavailable — falling back to in-memory event bus")
            return InMemoryEventBus()
        return RedisEventBus(redis_url)
    return InMemoryEventBus()
