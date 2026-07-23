"""Fall detection via keypoint kinematics.

A fall is *rapid, non-linear, and ends with the torso off-vertical*. Kneeling and bending are slower
and (for kneeling) leave the torso roughly upright. We therefore require BOTH a high downward torso
velocity AND an off-vertical final torso orientation before flagging a fall — this is what separates
a genuine fall from a fast squat or a deliberate bend.

Positions are normalized by frame height so thresholds are scale-invariant (near vs far worker).
Pure numpy — no torch/ultralytics — so it unit-tests against synthetic keypoint tracks.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from fieldpilot.core.types import (
    KP_LEFT_ANKLE,
    KP_LEFT_HIP,
    KP_LEFT_SHOULDER,
    KP_RIGHT_ANKLE,
    KP_RIGHT_HIP,
    KP_RIGHT_SHOULDER,
    FrameResult,
    HazardEvent,
    HazardType,
    PersonDetection,
    Severity,
)


@dataclass
class _Sample:
    t: float          # seconds (monotonic)
    y_norm: float     # torso-center vertical position, normalized to frame height
    angle_deg: float  # torso tilt from vertical (0 = upright, 90 = horizontal)


def _mean_point(det: PersonDetection, indices, min_conf: float) -> tuple[float, float] | None:
    pts = [det.kp(i) for i in indices]
    visible = [(x, y) for x, y, c in pts if c >= min_conf]
    if not visible:
        return None
    xs = sum(p[0] for p in visible) / len(visible)
    ys = sum(p[1] for p in visible) / len(visible)
    return xs, ys


def torso_metrics(det: PersonDetection, frame_h: int, min_conf: float) -> tuple[float, float] | None:
    """Return (torso_center_y_normalized, torso_tilt_from_vertical_deg) or None if too occluded."""

    shoulders = _mean_point(det, (KP_LEFT_SHOULDER, KP_RIGHT_SHOULDER), min_conf)
    hips = _mean_point(det, (KP_LEFT_HIP, KP_RIGHT_HIP), min_conf)
    if shoulders is None or hips is None:
        return None
    cy = (shoulders[1] + hips[1]) / 2.0
    # torso vector points from hips up to shoulders; angle vs the vertical axis.
    dx = shoulders[0] - hips[0]
    dy = shoulders[1] - hips[1]  # negative when shoulders are above hips (upright)
    angle = math.degrees(math.atan2(abs(dx), abs(dy) + 1e-6))
    return cy / max(frame_h, 1), angle


class FallDetector:
    def __init__(self, cfg):
        fd = cfg.section("fall_detection")
        self.buffer_frames = int(fd.get("buffer_frames", 15))
        self.velocity_thresh = float(fd.get("velocity_thresh", 1.2))
        self.angle_thresh = float(fd.get("torso_angle_thresh_deg", 55))
        self.min_kp = int(fd.get("min_confident_keypoints", 4))
        self.cooldown_s = float(fd.get("cooldown_s", 8))
        self.kp_conf = float(cfg.get("detection.keypoint_conf_min", 0.30))
        self._history: dict[int, deque[_Sample]] = {}
        self._last_alert: dict[int, float] = {}
        self.risk: dict[int, float] = {}  # live 0..1 fall-risk per track (for the overlay meter)

    def _confident_kp_count(self, det: PersonDetection) -> int:
        idxs = (
            KP_LEFT_SHOULDER, KP_RIGHT_SHOULDER, KP_LEFT_HIP, KP_RIGHT_HIP,
            KP_LEFT_ANKLE, KP_RIGHT_ANKLE,
        )
        return sum(1 for i in idxs if det.kp_visible(i, self.kp_conf))

    def update(self, result: FrameResult) -> list[HazardEvent]:
        events: list[HazardEvent] = []
        now = result.frame.ts_monotonic
        frame_h = result.frame.height
        seen_ids = set()

        for det in result.persons:
            seen_ids.add(det.track_id)
            if self._confident_kp_count(det) < self.min_kp:
                continue
            metrics = torso_metrics(det, frame_h, self.kp_conf)
            if metrics is None:
                continue
            y_norm, angle = metrics
            hist = self._history.setdefault(det.track_id, deque(maxlen=self.buffer_frames))
            hist.append(_Sample(now, y_norm, angle))

            # downward velocity in frame-heights per second (image y grows downward).
            velocity = 0.0
            if len(hist) >= max(3, self.buffer_frames // 2):
                oldest = hist[0]
                dt = now - oldest.t
                if dt > 1e-3:
                    velocity = (y_norm - oldest.y_norm) / dt

            # live fall-risk score in [0,1] (1.0 ≈ at the alerting threshold) for the overlay meter.
            self.risk[det.track_id] = min(
                1.0,
                0.5 * max(0.0, velocity) / self.velocity_thresh
                + 0.5 * angle / max(self.angle_thresh, 1.0),
            )

            if velocity >= self.velocity_thresh and angle >= self.angle_thresh:
                if now - self._last_alert.get(det.track_id, -1e9) < self.cooldown_s:
                    continue
                self._last_alert[det.track_id] = now
                events.append(
                    HazardEvent(
                        hazard_type=HazardType.FALL,
                        severity=Severity.HIGH,
                        message=f"Possible fall detected for worker {det.track_id}.",
                        frame_index=result.frame.index,
                        ts_monotonic=now,
                        track_id=det.track_id,
                        bbox=det.bbox,
                        meta={
                            "vertical_velocity": round(velocity, 3),
                            "torso_tilt_deg": round(angle, 1),
                            "window_s": round(dt, 3),
                        },
                    )
                )

        # forget tracks that have disappeared to keep memory bounded.
        for tid in list(self._history.keys()):
            if tid not in seen_ids and self._history[tid] and now - self._history[tid][-1].t > 3.0:
                self._history.pop(tid, None)
                self._last_alert.pop(tid, None)
                self.risk.pop(tid, None)
        return events
