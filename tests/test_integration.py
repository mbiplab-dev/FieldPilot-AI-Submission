"""End-to-end wiring: a fall flows detector → alert dispatch → durable store."""

from __future__ import annotations

from fieldpilot.alerts.dispatcher import AlertDispatcher
from fieldpilot.core.config import Config
from fieldpilot.logging_.store import EventStore
from fieldpilot.safety.fall import FallDetector
from tests.conftest import make_cfg, make_person, make_result

DT = 1.0 / 30.0


def _alerts_cfg(tmp_path) -> Config:
    cfg = make_cfg()
    cfg.as_dict()["alerts"] = {
        "cooldown_s": {"fall": 6},
        "earcons_dir": str(tmp_path / "earcons"),
        "tts": {"provider": "local", "cache_dir": str(tmp_path / "tts")},
        "haptics": {"enabled": True, "patterns": {"high": [400]}, "mobile_endpoint": None},
    }
    cfg.as_dict()["storage"] = {"sqlite_path": str(tmp_path / "f.db")}
    return cfg


def test_fall_reaches_alert_and_store(tmp_path):
    cfg = _alerts_cfg(tmp_path)
    detector = FallDetector(cfg)
    dispatcher = AlertDispatcher(cfg)
    dispatcher.dry_run = True  # keep the test silent; still exercises admission + latency
    store = EventStore(cfg.get("storage.sqlite_path"))

    t = 0.0
    admitted = 0
    for i in range(5):  # standing
        detector.update(make_result(t, i, [make_person(1, 120, 220)]))
        t += DT
    for i in range(5, 9):  # fall
        result = make_result(t, i, [make_person(1, 380, 390, shoulder_x=380, hip_x=250)])
        for ev in detector.update(result):
            store.record_event(ev, alerted=True)
            rec = dispatcher.dispatch(ev)
            admitted += int(rec.admitted)
            assert rec.latency_ms >= 0.0
        t += DT

    dispatcher.shutdown()
    assert admitted == 1, "exactly one fall alert should be admitted"
    assert store.count() == 1
    assert store.counts_by_type().get("fall") == 1
    store.close()
