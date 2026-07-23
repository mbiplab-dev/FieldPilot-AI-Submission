"""Fall detector: a rapid off-vertical drop fires; a slow kneel/fast squat does not."""

from __future__ import annotations

from fieldpilot.core.types import HazardType
from fieldpilot.safety.fall import FallDetector
from tests.conftest import make_cfg, make_person, make_result

DT = 1.0 / 30.0


def _run(detector: FallDetector, frames):
    fired = []
    for i, (t, person) in enumerate(frames):
        events = detector.update(make_result(t, i, [person]))
        fired.extend(e for e in events if e.hazard_type is HazardType.FALL)
    return fired


def test_rapid_fall_is_detected():
    det = FallDetector(make_cfg())
    frames = []
    t = 0.0
    # standing upright: shoulders above hips, torso vertical.
    for _ in range(5):
        frames.append((t, make_person(1, shoulder_y=120, hip_y=220)))
        t += DT
    # fall: torso goes horizontal (shoulders level with hips, large dx) and drops fast.
    for _ in range(4):
        frames.append((t, make_person(1, shoulder_y=380, hip_y=390, shoulder_x=380, hip_x=250)))
        t += DT
    fired = _run(det, frames)
    assert fired, "a rapid off-vertical drop should be flagged as a fall"
    assert fired[0].meta["torso_tilt_deg"] >= 55


def test_slow_kneel_is_not_a_fall():
    det = FallDetector(make_cfg())
    frames = []
    t = 0.0
    # torso stays vertical throughout; hips lower gradually (kneeling), over a long window.
    hip = 220.0
    shoulder = 120.0
    for _ in range(30):
        frames.append((t, make_person(2, shoulder_y=shoulder, hip_y=hip)))
        shoulder += 3.0
        hip += 3.0
        t += DT
    assert not _run(det, frames), "a slow, upright kneel must not be flagged as a fall"


def test_fast_squat_upright_is_not_a_fall():
    det = FallDetector(make_cfg())
    frames = []
    t = 0.0
    for _ in range(5):
        frames.append((t, make_person(3, shoulder_y=120, hip_y=220)))
        t += DT
    # fast downward motion but torso stays vertical (a squat), so angle stays low.
    for _ in range(4):
        frames.append((t, make_person(3, shoulder_y=300, hip_y=400)))
        t += DT
    assert not _run(det, frames), "fast but upright motion (squat) must not be a fall"


def test_cooldown_suppresses_repeat_falls():
    det = FallDetector(make_cfg(fall_detection={"cooldown_s": 5}))
    frames = []
    t = 0.0
    for _ in range(5):
        frames.append((t, make_person(4, shoulder_y=120, hip_y=220)))
        t += DT
    for _ in range(10):  # stay fallen
        frames.append((t, make_person(4, shoulder_y=380, hip_y=390, shoulder_x=380, hip_x=250)))
        t += DT
    fired = _run(det, frames)
    assert len(fired) == 1, "cooldown should collapse a sustained fall into a single alert"
