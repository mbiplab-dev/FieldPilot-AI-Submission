"""Hot-path cache for the trigger engine.

Production backend is Redis (shared across service replicas). Dev/tests use an in-memory
TTL cache with identical semantics, so the engine logic never changes with infrastructure.
Redis is imported lazily — the codebase runs with zero infra installed.
"""

from __future__ import annotations

import fnmatch
import json
import time
from typing import Any, Protocol

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.triggers.cache")


class TriggerCache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def scan(self, pattern: str) -> list[tuple[str, dict[str, Any]]]: ...


class InMemoryTriggerCache:
    """Dict + TTL sweep. Thread-unsafe by design: the engine is single-loop async."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[dict[str, Any], float]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires < time.time():
            del self._data[key]
            return None
        return dict(value)

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        self._data[key] = (dict(value), time.time() + ttl_s)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def scan(self, pattern: str) -> list[tuple[str, dict[str, Any]]]:
        now = time.time()
        out = []
        for key, (value, expires) in list(self._data.items()):
            if expires < now:
                del self._data[key]
                continue
            if fnmatch.fnmatchcase(key, pattern):
                out.append((key, dict(value)))
        return out


class RedisTriggerCache:
    """Redis hash-backed cache with per-key TTL."""

    _PREFIX = "fp:trigger:"

    def __init__(self, url: str) -> None:
        self.url = url
        self._redis = None

    async def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis  # lazy optional dependency

            self._redis = aioredis.from_url(self.url, decode_responses=True)
        return self._redis

    async def get(self, key: str) -> dict[str, Any] | None:
        r = await self._client()
        raw = await r.get(self._PREFIX + key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        r = await self._client()
        await r.set(self._PREFIX + key, json.dumps(value, default=str), ex=max(ttl_s, 1))

    async def delete(self, key: str) -> None:
        r = await self._client()
        await r.delete(self._PREFIX + key)

    async def scan(self, pattern: str) -> list[tuple[str, dict[str, Any]]]:
        r = await self._client()
        out = []
        async for full_key in r.scan_iter(match=self._PREFIX + pattern, count=200):
            raw = await r.get(full_key)
            if raw is None:
                continue
            try:
                out.append((full_key[len(self._PREFIX):], json.loads(raw)))
            except json.JSONDecodeError:
                continue
        return out


def create_cache(backend: str = "memory", redis_url: str = "redis://localhost:6379/0") -> TriggerCache:
    if backend == "redis":
        try:
            import redis.asyncio  # noqa: F401

            return RedisTriggerCache(redis_url)
        except ImportError:
            log.warning("redis package unavailable — falling back to in-memory trigger cache")
    return InMemoryTriggerCache()
