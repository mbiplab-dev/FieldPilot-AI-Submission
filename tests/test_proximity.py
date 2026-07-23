"""Proximity detector: worker near machinery fires; far worker does not."""

from __future__ import annotations

import numpy as np

from fieldpilot.core.config import Config
from fieldpilot.core.types import NUM_KEYPOINTS, HazardType, PersonDetection
from fieldpilot.safety.proximity import ProximityDetector, _rect_gap
from tests.conftest import make_result


def _cfg():
    return Config({
        "proximity": {"enabled": True, "danger_distance_frac": 0.10},
        "alerts": {"cooldown_s": {"proximity": 8}},
    })


def _person(track_id, bbox):
    return PersonDetection(track_id=track_id, bbox=bbox, conf=0.9,
                           keypoints=np.zeros((NUM_KEYPOINTS, 3), dtype=np.float32))


def test_rect_gap():
    assert _rect_gap((0, 0, 10, 10), (5, 5, 15, 15)) == 0.0      # overlapping
    assert _rect_gap((0, 0, 10, 10), (20, 0, 30, 10)) == 10.0    # 10px horizontal gap


def test_worker_near_machinery_fires():
    det = ProximityDetector(_cfg())
    # frame diag ~800px, thresh = 80px; gap ~10px -> too close.
    result = make_result(0.0, 0, [_person(1, (100, 100, 200, 300))])
    equipment = [{"label": "machinery", "bbox": (210, 100, 300, 300), "kind": "machinery"}]
    events = det.update(result, equipment)
    assert len(events) == 1
    assert events[0].hazard_type is HazardType.PROXIMITY


def test_worker_far_from_machinery_is_safe():
    det = ProximityDetector(_cfg())
    result = make_result(0.0, 0, [_person(1, (100, 100, 200, 300))])
    equipment = [{"label": "machinery", "bbox": (500, 100, 600, 300), "kind": "machinery"}]
    assert det.update(result, equipment) == []


def test_cones_do_not_trigger_proximity():
    det = ProximityDetector(_cfg())
    result = make_result(0.0, 0, [_person(1, (100, 100, 200, 300))])
    equipment = [{"label": "Safety Cone", "bbox": (205, 100, 240, 200), "kind": "cone"}]
    assert det.update(result, equipment) == []
