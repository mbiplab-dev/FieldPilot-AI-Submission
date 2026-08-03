"""Zone registry: seeding, validation, the update whitelist and the hot-path cache."""

from __future__ import annotations

import pytest

from fieldpilot.storage import DocStore
from fieldpilot.zones.service import (
    DEFAULT_ZONES,
    HAZARD_LEVELS,
    ZONES_TABLE,
    ZoneService,
    slugify,
)

DEFAULT_IDS = {"zone-a", "zone-b", "zone-c", "zone-d"}


@pytest.fixture
async def store(tmp_path):
    s = DocStore("sqlite", str(tmp_path / "zones.db"))
    await s.start([ZONES_TABLE])
    try:
        yield s
    finally:
        await s.stop()


@pytest.fixture
async def svc(store):
    service = ZoneService(store)
    await service.start()
    return service


# --------------------------------------------------------------------------- slugify


def test_slugify():
    assert slugify("Zone A — Foundation") == "zone-a-foundation"
    assert slugify("  Level 3 East  ") == "level-3-east"
    assert slugify("Bay/7 (North)") == "bay-7-north"
    assert slugify("!!!") == "zone"          # never returns an empty id
    assert slugify("Already-Slug") == "already-slug"


# --------------------------------------------------------------------------- seeding


async def test_defaults_seed_on_first_start(svc):
    rows = await svc.list()
    assert {z["zone_id"] for z in rows} == DEFAULT_IDS
    assert len(rows) == len(DEFAULT_ZONES)
    by_id = {z["zone_id"]: z for z in rows}
    assert by_id["zone-a"]["hazard_level"] == "high"
    assert by_id["zone-a"]["danger"] is True
    assert by_id["zone-c"]["hazard_level"] == "medium"
    assert by_id["zone-c"]["danger"] is False
    assert by_id["zone-d"]["hazard_level"] == "low"
    assert by_id["zone-d"]["active"] is True
    assert "edge protection" in by_id["zone-b"]["description"]


async def test_defaults_are_not_reseeded_when_rows_exist(store, svc):
    await svc.delete("zone-a")
    await svc.delete("zone-b")

    second = ZoneService(store)
    await second.start()
    assert {z["zone_id"] for z in await second.list()} == {"zone-c", "zone-d"}


async def test_restart_does_not_duplicate_or_reset_edits(store, svc):
    await svc.update("zone-c", {"hazard_level": "high", "name": "Renamed Yard"})
    second = ZoneService(store)
    await second.start()
    rows = await second.list()
    assert len(rows) == len(DEFAULT_ZONES)
    zone_c = next(z for z in rows if z["zone_id"] == "zone-c")
    assert zone_c["hazard_level"] == "high"
    assert zone_c["name"] == "Renamed Yard"
    assert second.hazard_level("zone-c") == "high"


async def test_seed_defaults_false_leaves_the_registry_empty(store):
    service = ZoneService(store)
    await service.start(seed_defaults=False)
    assert await service.list() == []
    assert service.cached("zone-a") is None


# --------------------------------------------------------------------------- create


async def test_create_slugifies_the_name_into_an_id(svc):
    created = await svc.create({"name": "Level 3 — East Wing"})
    assert created["zone_id"] == "level-3-east-wing"
    assert created["name"] == "Level 3 — East Wing"
    assert created["project_id"] == "default"
    assert created["hazard_level"] == "medium"
    assert created["danger"] is False
    assert created["active"] is True
    assert await svc.get("level-3-east-wing") is not None


async def test_create_honours_an_explicit_zone_id(svc):
    created = await svc.create({"zone_id": "custom-99", "name": "Anything"})
    assert created["zone_id"] == "custom-99"


async def test_create_defaults_danger_from_a_high_hazard_level(svc):
    high = await svc.create({"name": "Crane Pad", "hazard_level": "high"})
    assert high["danger"] is True
    low = await svc.create({"name": "Canteen", "hazard_level": "low"})
    assert low["danger"] is False
    # an explicit flag still wins
    override = await svc.create({"name": "Odd One", "hazard_level": "high", "danger": False})
    assert override["danger"] is False


async def test_create_rejects_a_blank_name(svc):
    for name in ("", "   ", None):
        with pytest.raises(ValueError, match="zone name is required"):
            await svc.create({"name": name})


async def test_create_rejects_an_unknown_hazard_level(svc):
    with pytest.raises(ValueError, match="hazard_level must be one of"):
        await svc.create({"name": "Bad Zone", "hazard_level": "extreme"})
    assert await svc.get("bad-zone") is None


async def test_create_accepts_every_allowed_hazard_level(svc):
    for level in HAZARD_LEVELS:
        created = await svc.create({"name": f"Zone {level}", "hazard_level": level.upper()})
        assert created["hazard_level"] == level


async def test_create_rejects_a_duplicate_id(svc):
    with pytest.raises(ValueError, match="zone 'zone-a' already exists"):
        await svc.create({"zone_id": "zone-a", "name": "Clashing"})
    # the collision must not have clobbered the original
    assert (await svc.get("zone-a"))["name"] == "Zone A — Foundation"


async def test_create_rejects_a_duplicate_derived_from_the_name(svc):
    await svc.create({"name": "Pump Room"})
    with pytest.raises(ValueError, match="already exists"):
        await svc.create({"name": "pump room"})


# --------------------------------------------------------------------------- update


async def test_update_only_applies_whitelisted_fields(svc):
    before = await svc.get("zone-a")
    updated = await svc.update(
        "zone-a",
        {
            "name": "Zone A (renamed)",
            "description": "new text",
            "created_at": 1.0,        # not whitelisted, and immutable in the store
            "zone_id": "hacked",      # not whitelisted — must not re-key the row
            "consumed_by": "nope",    # unknown field must be dropped entirely
        },
    )
    assert updated["name"] == "Zone A (renamed)"
    assert updated["description"] == "new text"
    assert updated["zone_id"] == "zone-a"
    assert updated["created_at"] == before["created_at"]
    assert "consumed_by" not in updated
    assert await svc.get("hacked") is None
    assert (await svc.get("zone-a"))["name"] == "Zone A (renamed)"


async def test_update_stamps_updated_at(svc):
    before = await svc.get("zone-a")
    updated = await svc.update("zone-a", {"description": "x"})
    assert updated["updated_at"] > before["updated_at"]


async def test_update_normalises_and_validates_hazard_level(svc):
    assert (await svc.update("zone-c", {"hazard_level": "HIGH"}))["hazard_level"] == "high"
    with pytest.raises(ValueError, match="hazard_level must be one of"):
        await svc.update("zone-c", {"hazard_level": "spicy"})
    assert (await svc.get("zone-c"))["hazard_level"] == "high"   # unchanged by the failure


async def test_update_can_toggle_danger_and_active(svc):
    updated = await svc.update("zone-c", {"danger": True, "active": False})
    assert updated["danger"] is True
    assert updated["active"] is False
    assert (await svc.get("zone-c"))["danger"] is True


async def test_update_of_a_missing_zone_returns_none(svc):
    assert await svc.update("no-such-zone", {"name": "x"}) is None


# --------------------------------------------------------------------------- reads / cache


async def test_cached_is_populated_by_start(svc):
    assert svc.cached("zone-a")["hazard_level"] == "high"
    assert svc.cached("nope") is None
    assert svc.cached(None) is None
    assert svc.cached("") is None


async def test_is_danger_zone(svc):
    assert svc.is_danger_zone("zone-a") is True
    assert svc.is_danger_zone("zone-b") is True
    assert svc.is_danger_zone("zone-c") is False
    assert svc.is_danger_zone("unknown-zone") is False
    assert svc.is_danger_zone(None) is False


async def test_hazard_level_defaults_to_medium_for_an_unknown_zone(svc):
    assert svc.hazard_level("zone-a") == "high"
    assert svc.hazard_level("zone-d") == "low"
    assert svc.hazard_level("unknown-zone") == "medium"
    assert svc.hazard_level(None) == "medium"


async def test_update_refreshes_the_cache(svc):
    assert svc.is_danger_zone("zone-c") is False
    await svc.update("zone-c", {"danger": True, "hazard_level": "high"})
    assert svc.is_danger_zone("zone-c") is True
    assert svc.hazard_level("zone-c") == "high"


async def test_create_populates_the_cache(svc):
    await svc.create({"name": "Hot Works", "hazard_level": "high"})
    assert svc.is_danger_zone("hot-works") is True
    assert svc.hazard_level("hot-works") == "high"


async def test_get_warms_the_cache_for_a_zone_created_elsewhere(store, svc):
    other = ZoneService(store)
    await other.start(seed_defaults=False)
    await other.create({"name": "Late Zone", "hazard_level": "high"})

    assert svc.cached("late-zone") is None      # this instance has not seen it yet
    await svc.get("late-zone")
    assert svc.is_danger_zone("late-zone") is True


async def test_delete_evicts_the_cache(svc):
    assert svc.is_danger_zone("zone-a") is True
    assert await svc.delete("zone-a") is True
    assert svc.cached("zone-a") is None
    assert svc.is_danger_zone("zone-a") is False
    assert svc.hazard_level("zone-a") == "medium"
    assert await svc.delete("zone-a") is False


# --------------------------------------------------------------------------- list filters


async def test_list_filters_by_project_and_active(svc):
    await svc.create({"name": "Riverside Pit", "project_id": "riverside"})
    await svc.create({"name": "Riverside Dormant", "project_id": "riverside", "active": False})

    riverside = await svc.list(project_id="riverside")
    assert {z["zone_id"] for z in riverside} == {"riverside-pit", "riverside-dormant"}

    active = await svc.list(project_id="riverside", active_only=True)
    assert {z["zone_id"] for z in active} == {"riverside-pit"}

    assert {z["zone_id"] for z in await svc.list(project_id="default")} == DEFAULT_IDS
