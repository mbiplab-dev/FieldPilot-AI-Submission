"""Operator-editable site settings, persisted and pushed to the edge.

The `hrm` branch kept these in a `settings` key/value table and a `tracked_items` table so a site
manager could turn individual PPE checks on and off, retune the confidence threshold, and switch
detector without editing YAML. That is the right idea; this is that idea on our storage layer and
our event bus.

`config.yaml` supplies the boot defaults. Once an operator changes a value it lives here and wins,
so a restart does not silently revert the site's configuration.

Changes are published on the bus as `control.settings`, which the edge applies to its running
detectors — the same pattern `control.inspection` already uses. Settings never reach the models
directly; nothing in this file talks to a detector.
"""

from __future__ import annotations

import time
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.backend.settings")

SETTINGS_TABLE = TableSpec(
    "site_settings",
    key="key",
    columns=(Column("value"), Column("created_at", "real"), Column("updated_at", "real")),
    order_by="key",
)

#: PPE items the operator can independently enable or disable
TRACKED_ITEMS: tuple[str, ...] = ("helmet", "vest", "gloves", "boots", "goggles")

TOPIC_SETTINGS = "control.settings"

_CONFIDENCE_MIN, _CONFIDENCE_MAX = 0.20, 0.85


class SettingsService:
    """Key/value site settings with typed accessors and bus notification."""

    def __init__(self, store: DocStore, bus: Any = None) -> None:
        self._table = store.table(SETTINGS_TABLE)
        self._bus = bus
        self._cache: dict[str, Any] = {}
        self._defaults: dict[str, Any] = {}

    async def start(self, defaults: dict[str, Any]) -> None:
        """Seed from config.yaml, then let any persisted operator overrides win."""

        self._defaults = dict(defaults)
        rows = await self._table.list(limit=200)
        self._cache = {**self._defaults, **{r["key"]: r.get("value_json") for r in rows}}
        log.info(
            "site settings ready (%d persisted override(s))",
            sum(1 for r in rows if r["key"] in self._defaults),
        )

    # -- reads -----------------------------------------------------------------

    def all(self) -> dict[str, Any]:
        return dict(self._cache)

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, self._defaults.get(key, default))

    def tracked_items(self) -> dict[str, bool]:
        stored = self.get("tracked_items") or {}
        return {item: bool(stored.get(item, True)) for item in TRACKED_ITEMS}

    def enabled_items(self) -> list[str]:
        return [item for item, on in self.tracked_items().items() if on]

    # -- writes ----------------------------------------------------------------

    async def set(self, key: str, value: Any, *, publish: bool = True) -> Any:
        now = time.time()
        await self._table.put({"key": key, "value": str(value)[:200],
                               "value_json": value, "updated_at": now})
        self._cache[key] = value
        if publish and self._bus is not None:
            await self._bus.publish(TOPIC_SETTINGS, {key: value})
        return value

    async def set_tracked_item(self, item: str, enabled: bool) -> dict[str, bool]:
        if item not in TRACKED_ITEMS:
            raise ValueError(f"unknown PPE item {item!r}; expected one of {TRACKED_ITEMS}")
        items = self.tracked_items()
        items[item] = bool(enabled)
        await self.set("tracked_items", items)
        log.info("tracked item %s -> %s", item, "on" if enabled else "off")
        return items

    async def set_monitoring(
        self, *, confidence_threshold: float | None = None, pose_enabled: bool | None = None
    ) -> dict[str, Any]:
        if confidence_threshold is not None:
            if not _CONFIDENCE_MIN <= float(confidence_threshold) <= _CONFIDENCE_MAX:
                raise ValueError(
                    f"confidence_threshold must be between {_CONFIDENCE_MIN} and {_CONFIDENCE_MAX}"
                )
            await self.set("confidence_threshold", round(float(confidence_threshold), 3))
        if pose_enabled is not None:
            await self.set("pose_enabled", bool(pose_enabled))
        return {
            "confidence_threshold": self.get("confidence_threshold"),
            "pose_enabled": self.get("pose_enabled"),
        }

    async def set_selected_model(self, key: str) -> str:
        await self.set("selected_model", key)
        log.info("selected detector -> %s", key)
        return key
