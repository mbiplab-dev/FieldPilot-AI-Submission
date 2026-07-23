"""Attention state machine: dwell → NOTICED; ignored hazard → UNNOTICED → ESCALATED."""

from __future__ import annotations

from fieldpilot.core.types import AttentionState
from fieldpilot.safety.attention import HazardAttention


def _machine(onset=0.0):
    return HazardAttention(
        hazard_id="h1",
        onset_ms=onset,
        dwell_ms=600,
        glance_ms=200,
        unnoticed_after_ms=2500,
        escalate_after_ms=6000,
    )


def test_sustained_dwell_becomes_noticed():
    m = _machine()
    assert m.update(0, True) is AttentionState.PASSIVE
    assert m.update(300, True) is AttentionState.PASSIVE
    assert m.update(650, True) is AttentionState.NOTICED  # 650 ms contiguous ≥ 600 ms dwell


def test_glance_does_not_count():
    m = _machine()
    m.update(0, True)
    m.update(150, False)      # gaze left before dwell threshold → resets
    m.update(300, True)       # new dwell window starts here
    # only 300 ms contiguous by t=550, still short of 600 ms.
    assert m.update(550, True) is not AttentionState.NOTICED


def test_ignored_hazard_escalates():
    m = _machine()
    assert m.update(0, False) is AttentionState.PASSIVE
    assert m.update(2600, False) is AttentionState.UNNOTICED
    assert m.update(6100, False) is AttentionState.ESCALATED


def test_notice_is_terminal_even_if_gaze_leaves():
    m = _machine()
    m.update(0, True)
    m.update(700, True)  # NOTICED
    assert m.state is AttentionState.NOTICED
    # later frames with no gaze must not downgrade an already-acknowledged hazard.
    assert m.update(9000, False) is AttentionState.NOTICED
