"""Shared test helpers."""

from __future__ import annotations

import numpy as np

from fieldpilot.core.config import Config
from fieldpilot.core.types import (
    KP_LEFT_ANKLE,
    KP_LEFT_HIP,
    KP_LEFT_SHOULDER,
    KP_RIGHT_ANKLE,
    KP_RIGHT_HIP,
    KP_RIGHT_SHOULDER,
    NUM_KEYPOINTS,
    Frame,
    FrameResult,
    PersonDetection,
)

FRAME_H = 480
FRAME_W = 640


def make_cfg(**sections) -> Config:
    base = {
        "detection": {"keypoint_conf_min": 0.3, "conf_min": 0.35},
        "fall_detection": {
            "buffer_frames": 8,
            "velocity_thresh": 1.0,
            "torso_angle_thresh_deg": 55,
            "min_confident_keypoints": 4,
            "cooldown_s": 5,
        },
        "attention": {
            "dwell_ms": 600,
            "glance_ms": 200,
            "unnoticed_after_ms": 2500,
            "escalate_after_ms": 6000,
            "gaze_cone_deg": 25,
        },
    }
    for name, override in sections.items():
        base.setdefault(name, {}).update(override)
    return Config(base)


def make_person(
    track_id: int,
    shoulder_y: float,
    hip_y: float,
    shoulder_x: float = 320,
    hip_x: float = 320,
    conf: float = 0.9,
) -> PersonDetection:
    kp = np.zeros((NUM_KEYPOINTS, 3), dtype=np.float32)
    kp[KP_LEFT_SHOULDER] = [shoulder_x - 20, shoulder_y, conf]
    kp[KP_RIGHT_SHOULDER] = [shoulder_x + 20, shoulder_y, conf]
    kp[KP_LEFT_HIP] = [hip_x - 15, hip_y, conf]
    kp[KP_RIGHT_HIP] = [hip_x + 15, hip_y, conf]
    kp[KP_LEFT_ANKLE] = [hip_x - 15, hip_y + 120, conf]
    kp[KP_RIGHT_ANKLE] = [hip_x + 15, hip_y + 120, conf]
    x1 = min(shoulder_x, hip_x) - 40
    x2 = max(shoulder_x, hip_x) + 40
    y1 = min(shoulder_y, hip_y) - 30
    y2 = max(shoulder_y, hip_y) + 140
    return PersonDetection(
        track_id=track_id,
        bbox=(float(x1), float(y1), float(x2), float(y2)),
        conf=conf,
        keypoints=kp,
    )


def make_result(t: float, index: int, persons: list[PersonDetection]) -> FrameResult:
    img = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    frame = Frame(index=index, ts_monotonic=t, image=img)
    return FrameResult(frame=frame, persons=persons)
