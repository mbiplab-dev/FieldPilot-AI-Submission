"""Config env-overrides and the SQLite event store (idempotency + offline queue)."""

from __future__ import annotations

from fieldpilot.core.config import load_config
from fieldpilot.core.types import HazardEvent, HazardType, Severity
from fieldpilot.logging_.store import EventStore, event_from_row


def test_env_override_coerces_types(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "app:\n  perspective: EXO\ndetection:\n  track_buffer: 30\n  conf_min: 0.35\n"
    )
    monkeypatch.setenv("FIELDPILOT_APP__PERSPECTIVE", "BOTH")
    monkeypatch.setenv("FIELDPILOT_DETECTION__TRACK_BUFFER", "50")
    cfg = load_config(cfg_file)
    assert cfg.get("app.perspective") == "BOTH"
    assert cfg.get("detection.track_buffer") == 50  # coerced to int, not "50"
    assert cfg.get("detection.conf_min") == 0.35


def _event(eid="e1"):
    return HazardEvent(
        hazard_type=HazardType.FALL,
        severity=Severity.HIGH,
        message="test fall",
        frame_index=1,
        ts_monotonic=1.0,
        track_id=7,
        bbox=(1.0, 2.0, 3.0, 4.0),
        id=eid,
    )


def test_store_is_idempotent_on_event_id(tmp_path):
    store = EventStore(tmp_path / "f.db")
    ev = _event()
    store.record_event(ev, alerted=True)
    store.record_event(ev, alerted=True)  # replay from offline queue must not duplicate
    assert store.count() == 1
    store.close()


def test_offline_queue_sync_lifecycle(tmp_path):
    store = EventStore(tmp_path / "f.db")
    store.record_event(_event("a"))
    store.record_event(_event("b"))
    pending = store.unsynced()
    assert {r["id"] for r in pending} == {"a", "b"}
    # round-trips back into a HazardEvent for the flusher.
    rehydrated = event_from_row(pending[0])
    assert rehydrated.hazard_type is HazardType.FALL
    assert rehydrated.bbox == (1.0, 2.0, 3.0, 4.0)

    store.mark_synced(["a", "b"])
    assert store.unsynced() == []
    store.close()
