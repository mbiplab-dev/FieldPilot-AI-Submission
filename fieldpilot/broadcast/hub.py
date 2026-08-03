"""WebSocket hub with zone routing, fanned out over the event bus."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fieldpilot.events.bus import EventBus
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.broadcast")

BROADCAST_PATTERN = "broadcast.*"
ALL_ZONES = "all"

#: severity a primary alert is downgraded to when advised to nearby workers
ADVISORY_SEVERITY = "low"


@dataclass
class Client:
    """One connected socket. `zone` scopes what a device receives."""

    ws: Any
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: str = "dashboard"            # dashboard | device
    zone: str | None = None            # None / "all" = every zone
    worker_id: str | None = None
    connected_at: float = field(default_factory=time.time)

    @property
    def listens_to_all_zones(self) -> bool:
        """Only a dashboard may span zones.

        Dashboards are the supervisory view, so an unpinned one sees every zone. A *device*
        without a zone must NOT become a site-wide receiver — that is exactly the cross-site
        noise PRD §4.4 exists to prevent — so it receives only messages carrying no zone.
        """

        return self.kind == "dashboard" and self.zone in (None, "", ALL_ZONES)

    def wants(self, *, zone: str | None, audience: str, exclude: str | None) -> bool:
        if self.client_id == exclude:
            return False
        if audience != "all" and audience != self.kind:
            return False
        if zone is None:
            return True
        return self.listens_to_all_zones or self.zone == zone

    def describe(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id, "kind": self.kind, "zone": self.zone,
            "worker_id": self.worker_id, "connected_at": self.connected_at,
        }


class BroadcastHub:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._clients: dict[str, Client] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self.delivered = 0
        self.dropped = 0

    async def start(self) -> None:
        if self._started:
            return
        await self.bus.subscribe(BROADCAST_PATTERN, self._on_bus_message)
        self._started = True
        log.info("broadcast hub started (pattern %s)", BROADCAST_PATTERN)

    # -- connections -----------------------------------------------------------

    async def connect(
        self, ws: Any, *, kind: str = "dashboard", zone: str | None = None,
        worker_id: str | None = None,
    ) -> Client:
        client = Client(ws=ws, kind=kind if kind in ("dashboard", "device") else "dashboard",
                        zone=zone, worker_id=worker_id)
        async with self._lock:
            self._clients[client.client_id] = client
        log.info("ws connect %s kind=%s zone=%s (clients=%d)",
                 client.client_id, client.kind, client.zone, len(self._clients))
        return client

    async def disconnect(self, client: Client) -> None:
        async with self._lock:
            self._clients.pop(client.client_id, None)
        log.info("ws disconnect %s (clients=%d)", client.client_id, len(self._clients))

    def clients(self) -> list[dict[str, Any]]:
        return [c.describe() for c in self._clients.values()]

    def stats(self) -> dict[str, Any]:
        by_zone: dict[str, int] = {}
        for c in self._clients.values():
            key = c.zone or ALL_ZONES
            by_zone[key] = by_zone.get(key, 0) + 1
        return {
            "connected": len(self._clients),
            "devices": sum(1 for c in self._clients.values() if c.kind == "device"),
            "dashboards": sum(1 for c in self._clients.values() if c.kind == "dashboard"),
            "by_zone": by_zone,
            "delivered": self.delivered,
            "dropped": self.dropped,
        }

    # -- publishing ------------------------------------------------------------

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        zone: str | None = None,
        audience: str = "all",
        exclude: str | None = None,
    ) -> None:
        """Publish to every replica via the bus. Local delivery happens on receipt."""

        await self.bus.publish(f"broadcast.{topic}", {
            "topic": topic,
            "zone": zone,
            "audience": audience,
            "exclude": exclude,
            "ts": time.time(),
            "data": data,
        })

    async def advise_zone(
        self, alert: dict[str, Any], *, exclude_client: str | None = None
    ) -> None:
        """Send the PRD's secondary, lower-priority advisory to the alert's zone."""

        zone = alert.get("zone")
        if not zone:
            # Without a zone there is no "identical geographic zone" to scope to; broadcasting
            # site-wide would be exactly the noise §4.4 exists to prevent.
            log.debug("advisory skipped: alert %s has no zone", alert.get("alert_id"))
            return
        await self.publish(
            "advisory",
            {
                "alert_id": alert.get("alert_id"),
                "event_type": alert.get("event_type"),
                "origin_worker_id": alert.get("worker_id"),
                "zone": zone,
                "severity": ADVISORY_SEVERITY,
                "original_severity": alert.get("severity"),
                "message": _advisory_text(alert),
            },
            zone=zone,
            audience="device",
            exclude=exclude_client,
        )

    # -- delivery --------------------------------------------------------------

    async def _on_bus_message(self, topic: str, message: dict[str, Any]) -> None:
        await self._deliver(
            topic=str(message.get("topic") or topic.removeprefix("broadcast.")),
            zone=message.get("zone"),
            audience=str(message.get("audience") or "all"),
            exclude=message.get("exclude"),
            data=message.get("data") or {},
            ts=float(message.get("ts") or time.time()),
        )

    async def _deliver(
        self, *, topic: str, zone: str | None, audience: str,
        exclude: str | None, data: dict[str, Any], ts: float,
    ) -> None:
        frame = {"topic": topic, "zone": zone, "ts": ts, "data": data}
        targets = [
            c for c in list(self._clients.values())
            if c.wants(zone=zone, audience=audience, exclude=exclude)
        ]
        if not targets:
            return
        results = await asyncio.gather(
            *(self._send(c, frame) for c in targets), return_exceptions=True
        )
        stale = [c for c, r in zip(targets, results, strict=True) if isinstance(r, Exception)]
        for client in stale:
            self.dropped += 1
            await self.disconnect(client)

    async def _send(self, client: Client, frame: dict[str, Any]) -> None:
        await client.ws.send_json(frame)
        self.delivered += 1


def _advisory_text(alert: dict[str, Any]) -> str:
    etype = str(alert.get("event_type") or "hazard").replace("_", " ")
    worker = alert.get("worker_id")
    who = f"worker {worker}" if worker else "a worker"
    return f"Advisory: {etype} involving {who} in your zone. Stay alert."
