"""Zone registry: CRUD plus the lookups the rest of the platform needs.

Zones carry a `hazard_level` that rule conditions can read (`context.zone_hazard_level`) and a
`danger` flag that makes "no helmet in a danger zone" a property of the site rather than of a
proximity alert happening to be open.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.zones")

ZONES_TABLE = TableSpec(
    "zones",
    key="zone_id",
    columns=(
        Column("name"),
        Column("project_id", indexed=True),
        Column("hazard_level"),          # low | medium | high
        Column("danger", "bool", indexed=True),
        Column("active", "bool", indexed=True),
        Column("description"),
        Column("created_at", "real"),
        Column("updated_at", "real"),
    ),
)

HAZARD_LEVELS = ("low", "medium", "high")


@dataclass
class Zone:
    zone_id: str
    name: str
    project_id: str = "default"
    hazard_level: str = "medium"
    danger: bool = False
    active: bool = True
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_ZONES: tuple[Zone, ...] = (
    Zone("zone-a", "Zone A — Foundation", hazard_level="high", danger=True,
         description="Excavation and rebar placement. Heavy plant operating."),
    Zone("zone-b", "Zone B — Level 3 Slab", hazard_level="high", danger=True,
         description="Working at height; edge protection required."),
    Zone("zone-c", "Zone C — Material Yard", hazard_level="medium",
         description="Deliveries and lifting. Vehicle movement."),
    Zone("zone-d", "Zone D — Site Office", hazard_level="low",
         description="Welfare and site office. PPE optional indoors."),
)

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.strip().lower()).strip("-") or "zone"


class ZoneService:
    """Zone CRUD over a `DocStore`, with an in-process cache for hot-path lookups."""

    def __init__(self, store: DocStore) -> None:
        self._table = store.table(ZONES_TABLE)
        self._cache: dict[str, dict[str, Any]] = {}

    async def start(self, *, seed_defaults: bool = True) -> None:
        existing = await self._table.list(limit=500)
        if not existing and seed_defaults:
            for zone in DEFAULT_ZONES:
                await self._table.put(zone.to_dict())
            log.info("seeded %d default zones", len(DEFAULT_ZONES))
            existing = await self._table.list(limit=500)
        self._cache = {z["zone_id"]: z for z in existing}

    # -- reads -----------------------------------------------------------------

    async def list(self, *, project_id: str | None = None,
                   active_only: bool = False) -> list[dict[str, Any]]:
        where: dict[str, Any] = {}
        if project_id:
            where["project_id"] = project_id
        if active_only:
            where["active"] = 1
        return await self._table.list(where=where or None, limit=500, descending=False)

    async def get(self, zone_id: str) -> dict[str, Any] | None:
        zone = await self._table.get(zone_id)
        if zone is not None:
            self._cache[zone_id] = zone
        return zone

    def cached(self, zone_id: str | None) -> dict[str, Any] | None:
        """Non-async lookup for hot paths (rule context). May be slightly stale."""

        return self._cache.get(zone_id) if zone_id else None

    def is_danger_zone(self, zone_id: str | None) -> bool:
        zone = self.cached(zone_id)
        return bool(zone and zone.get("danger"))

    def hazard_level(self, zone_id: str | None) -> str:
        zone = self.cached(zone_id)
        return str(zone.get("hazard_level", "medium")) if zone else "medium"

    # -- writes ----------------------------------------------------------------

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("zone name is required")
        level = str(data.get("hazard_level", "medium")).lower()
        if level not in HAZARD_LEVELS:
            raise ValueError(f"hazard_level must be one of {HAZARD_LEVELS}")
        zone = Zone(
            zone_id=str(data.get("zone_id") or slugify(name)),
            name=name,
            project_id=str(data.get("project_id", "default")),
            hazard_level=level,
            danger=bool(data.get("danger", level == "high")),
            active=bool(data.get("active", True)),
            description=str(data.get("description", "")),
        )
        if await self._table.get(zone.zone_id) is not None:
            raise ValueError(f"zone {zone.zone_id!r} already exists")
        stored = await self._table.put(zone.to_dict())
        self._cache[zone.zone_id] = stored
        log.info("zone created: %s (%s)", zone.zone_id, zone.hazard_level)
        return stored

    async def update(self, zone_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "project_id", "hazard_level", "danger", "active", "description"}
        patch = {k: v for k, v in changes.items() if k in allowed}
        if "name" in patch:
            # validated on the same terms as `create` — an update must not be able to leave a
            # zone nameless when creation would have rejected it
            name = str(patch["name"]).strip()
            if not name:
                raise ValueError("zone name cannot be blank")
            patch["name"] = name
        if "hazard_level" in patch:
            level = str(patch["hazard_level"]).lower()
            if level not in HAZARD_LEVELS:
                raise ValueError(f"hazard_level must be one of {HAZARD_LEVELS}")
            patch["hazard_level"] = level
        patch["updated_at"] = time.time()
        updated = await self._table.patch(zone_id, patch)
        if updated is not None:
            self._cache[zone_id] = updated
        return updated

    async def delete(self, zone_id: str) -> bool:
        self._cache.pop(zone_id, None)
        return await self._table.delete(zone_id)
