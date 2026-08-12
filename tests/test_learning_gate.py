from __future__ import annotations

from fieldpilot.learning.service import _promotion_reasons

BASE = {"precision": 0.8, "recall": 0.7, "map50": 0.75, "map50_95": 0.44}


def test_candidate_must_hold_map_and_recall_not_only_map50():
    reasons = _promotion_reasons(
        BASE,
        {"precision": 0.9, "recall": 0.6, "map50": 0.76, "map50_95": 0.40},
        min_map50_delta=0.0,
        recall_tolerance=0.01,
    )

    assert any("mAP50-95 regressed" in reason for reason in reasons)
    assert any("recall regressed" in reason for reason in reasons)


def test_balanced_non_regressing_candidate_is_promotable():
    reasons = _promotion_reasons(
        BASE,
        {"precision": 0.81, "recall": 0.695, "map50": 0.77, "map50_95": 0.45},
        min_map50_delta=0.01,
        recall_tolerance=0.01,
    )

    assert reasons == []
