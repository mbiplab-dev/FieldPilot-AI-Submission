"""Perspective abstraction: EGO vs EXO gaze.

This module fixes the central conceptual error in the source PRD, which tried to infer the *wearer's*
attention from `solvePnP` on the wearer's own face — impossible, since a head-mounted camera can
never see its wearer's face.

- **EXO** (default with a webcam/phone aimed at a scene): the people in frame are *observed* workers.
  Their gaze is estimated from their visible face keypoints (a coarse head-yaw heuristic in M1;
  replaced by full `solvePnP` head-pose in M2).
- **EGO** (wearer's own state): the wearer looks roughly where the camera points, so a hazard near
  the frame centre is "being looked at". True yaw/pitch will come from the glasses/phone IMU
  (`set_orientation`); until that stream exists we use the optical-axis proxy.
- **BOTH**: a hazard counts as seen if either the observed worker or the wearer is looking at it.

`gaze_fn(person, bbox) -> bool` is the single interface the AttentionTracker consumes.
"""

from __future__ import annotations

import math

from fieldpilot.core.types import (
    KP_LEFT_EAR,
    KP_LEFT_EYE,
    KP_NOSE,
    KP_RIGHT_EAR,
    KP_RIGHT_EYE,
    PersonDetection,
    Perspective,
)


class GazeEstimator:
    def __init__(self, cfg):
        self.mode = Perspective(str(cfg.get("app.perspective", "EXO")).upper())
        self.cone_deg = float(cfg.get("attention.gaze_cone_deg", 25))
        self.kp_conf = float(cfg.get("detection.keypoint_conf_min", 0.30))
        # frame size is learned from the first frame so the ego centre-cone can be scaled.
        self._frame_wh: tuple[int, int] | None = None
        # optional wearer orientation (yaw, pitch) in degrees, pushed from an IMU in EGO/BOTH mode.
        self._wearer_yaw_deg: float | None = None

    def set_frame_size(self, width: int, height: int) -> None:
        self._frame_wh = (width, height)

    def set_orientation(self, yaw_deg: float, pitch_deg: float = 0.0) -> None:
        """Hook for the glasses/phone IMU stream (Milestone 2). Unused by the webcam proxy."""

        self._wearer_yaw_deg = yaw_deg

    # ---- EXO: observed worker's head yaw vs the target direction --------------------------------
    def _exo_looking_at(self, person: PersonDetection, bbox) -> bool:
        nose = person.kp(KP_NOSE)
        le, re = person.kp(KP_LEFT_EYE), person.kp(KP_RIGHT_EYE)
        lear, rear = person.kp(KP_LEFT_EAR), person.kp(KP_RIGHT_EAR)
        if nose[2] < self.kp_conf:
            return False

        # horizontal reference: midpoint of whichever face pair is visible, plus a width scale.
        if le[2] >= self.kp_conf and re[2] >= self.kp_conf:
            mid_x = (le[0] + re[0]) / 2.0
            width = abs(le[0] - re[0])
        elif lear[2] >= self.kp_conf and rear[2] >= self.kp_conf:
            mid_x = (lear[0] + rear[0]) / 2.0
            width = abs(lear[0] - rear[0])
        else:
            return False
        if width < 1e-3:
            return False

        # nose offset from face midline encodes head yaw. Positive → facing image-left.
        yaw_ratio = (mid_x - nose[0]) / width          # roughly [-1, 1] over ~±60°
        yaw_deg = max(-70.0, min(70.0, yaw_ratio * 70.0))

        # horizontal direction from the worker's face to the hazard, in image space.
        fx = nose[0]
        cx = (bbox[0] + bbox[2]) / 2.0
        target_deg = math.degrees(math.atan2(-(cx - fx), 1.0))  # signed horizontal bearing
        # compare head yaw bearing to target bearing within the cone.
        return abs(_wrap_deg(target_deg - yaw_deg)) <= self.cone_deg + 10.0

    # ---- EGO: wearer looks where the camera points ---------------------------------------------
    def _ego_looking_at(self, bbox) -> bool:
        if self._frame_wh is None:
            return False
        w, h = self._frame_wh
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        # angular offset of the target from the optical axis (assume ~60° horizontal FOV).
        off_x_deg = ((cx / w) - 0.5) * 60.0
        off_y_deg = ((cy / h) - 0.5) * 45.0
        if self._wearer_yaw_deg is not None:
            off_x_deg -= self._wearer_yaw_deg
        return math.hypot(off_x_deg, off_y_deg) <= self.cone_deg

    def looking_at(self, person: PersonDetection, bbox) -> bool:
        if self.mode is Perspective.EXO:
            return self._exo_looking_at(person, bbox)
        if self.mode is Perspective.EGO:
            return self._ego_looking_at(bbox)
        return self._ego_looking_at(bbox) or self._exo_looking_at(person, bbox)

    def gaze_fn(self):
        """Return the bound callable the AttentionTracker expects."""

        return self.looking_at


def _wrap_deg(angle: float) -> float:
    """Wrap to [-180, 180]."""

    return (angle + 180.0) % 360.0 - 180.0
