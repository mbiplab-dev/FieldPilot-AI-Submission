"""Inspection detector — trained structural-damage model → crack events.

The real model is loaded lazily; tests inject a lightweight fake YOLO model so the
detection logic + class→severity mapping + grid dedup run with zero ML deps.
"""

from __future__ import annotations

from fieldpilot.core.types import HazardType
from fieldpilot.inspection.detector import InspectionDetector
from tests.conftest import make_result


class _Tensor1:
    """1-element tensor-like: int(t) → the single element; t[0] → element."""

    def __init__(self, v):
        self._v = [v]

    def __int__(self):
        return int(self._v[0])

    def __float__(self):
        return float(self._v[0])

    def __getitem__(self, i):
        return self._v[i]


class FakeBox:
    def __init__(self, cls, xyxy, conf):
        self.cls = _Tensor1(cls)
        self.xyxy = [xyxy]
        self.conf = _Tensor1(conf)


class FakePred:
    def __init__(self, boxes):
        self.boxes = boxes


class FakeModel:
    def __init__(self, boxes):
        self.names = {i: n for i, n in enumerate(
            ["Minorrotation", "Moderaterotation", "Severerotation"])}
        self._boxes = boxes

    def predict(self, image, **kw):
        return [FakePred(self._boxes)]


def cfg(**ins):
    from fieldpilot.core.config import Config
    base = {
        "inspection": {"enabled": True, "conf_min": 0.30, "frame_skip": 1, **ins},
        "detection": {"device": "cpu"},
    }
    return Config(base)


def test_disabled_returns_nothing():
    d = InspectionDetector(cfg(enabled=False))
    assert d.enabled is False
    assert d.update(make_result(0, 0, [])) == []


def test_missing_model_marks_unavailable():
    d = InspectionDetector(cfg(model="does-not-exist.pt", enabled=True))
    assert d.available is False
    assert d.set_enabled(True) is False  # cannot turn on without a model


def test_class_severity_mapping():
    d = InspectionDetector(cfg())
    d._model = FakeModel([])
    d.available = True
    boxes = [
        FakeBox(0, (10, 10, 80, 80), 0.9),     # Minorrotation
        FakeBox(1, (10, 10, 80, 80), 0.9),     # Moderaterotation
        FakeBox(2, (10, 10, 80, 80), 0.9),     # Severerotation (>0.85 → rule fires)
        FakeBox(99, (10, 10, 80, 80), 0.9),    # unknown → default 0.50
    ]
    d._model = FakeModel(boxes)
    events = d.update(make_result(0, 0, []))
    scores = sorted(float(e.meta["severity_score"]) for e in events)
    assert scores == sorted([0.45, 0.70, 0.92, 0.50])
    assert all(e.hazard_type is HazardType.CRACK for e in events)


def test_grid_dedup_key_distinguishes_locations():
    d = InspectionDetector(cfg())
    d._model = FakeModel([FakeBox(2, (10, 10, 90, 90), 0.95)])  # grid cell 0:0
    d.available = True
    e1 = d.update(make_result(0, 0, []))[0]
    d._model = FakeModel([FakeBox(2, (500, 400, 580, 480), 0.95)])  # grid cell ~5:4
    e2 = d.update(make_result(1, 1, []))[0]
    assert e1.meta["dedup_key"] != e2.meta["dedup_key"]


def test_frame_skip_runs_every_nth_frame():
    d = InspectionDetector(cfg(frame_skip=3))
    d._model = FakeModel([FakeBox(0, (10, 10, 80, 80), 0.9)])
    d.available = True
    # frames 1, 2 skipped (nonzero mod 3); frame 3 runs
    assert d.update(make_result(0, 0, [])) == []
    assert d.update(make_result(1, 1, [])) == []
    assert len(d.update(make_result(2, 2, []))) == 1


def test_set_enabled_toggles_runtime():
    d = InspectionDetector(cfg(enabled=False))
    d.available = True
    assert d.set_enabled(True) is True
    assert d.enabled is True
    assert d.set_enabled(False) is False
    assert d.enabled is False