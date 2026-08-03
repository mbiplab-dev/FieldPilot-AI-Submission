"""Perspective abstraction: EGO vs EXO gaze.

This module fixes the central conceptual error in the source PRD, which tried to infer the *wearer's*
attention from `solvePnP` on the wearer's own face — impossible, since a head-mounted camera can
never see its wearer's face.

- **EXO** (default with a webcam/phone aimed at a scene): the people in frame are *observed*
  workers. Their gaze comes from a real `cv2.solvePnP` head pose fitted to the five facial keypoints
  YOLO-Pose emits (nose, both eyes, both ears) against a generic 3D head model. When too few
  keypoints are confident for PnP to be well posed we degrade to the M1 nose-offset heuristic rather
  than guessing.
- **EGO** (wearer's own state): the wearer looks roughly where the camera points, so a hazard near
  the frame centre is "being looked at". True yaw/pitch will come from the glasses/phone IMU
  (`set_orientation`); until that stream exists we use the optical-axis proxy.
- **BOTH**: a hazard counts as seen if either the observed worker or the wearer is looking at it.

`gaze_fn(person, bbox) -> bool` is the single interface the AttentionTracker consumes.

Sign conventions (all angles in degrees, all "public" pose values follow this)
----------------------------------------------------------------------------
The camera frame is OpenCV standard: +X to the image right, +Y down the image, +Z away from the
camera into the scene. The head model is authored so that the identity rotation is a head looking
straight down the optical axis back at the camera (its facing direction is model -Z).

- ``yaw_deg``   > 0 → head turned toward the **image right** (increasing pixel x), i.e. toward the
                      worker's own left, because we see them mirrored.
- ``pitch_deg`` > 0 → head tilted **up** (toward decreasing pixel y).
- ``roll_deg``  > 0 → head tilted **clockwise as seen in the image**.

Bearings to a hazard use the same signs: positive horizontal bearing = hazard is to the image right
of the worker's face, positive vertical bearing = hazard is above it. A worker is "looking at" a
hazard when ``hypot(bearing_x - yaw, bearing_y - pitch) <= gaze_cone_deg``.

Depth caveat: a single camera gives no range to the hazard, so the bearing is the angle subtended at
the camera between the worker's face ray and the hazard ray. That is exact for a distant hazard and
degrades for a hazard very close to the worker — the standard monocular gaze-following
approximation, and the reason the cone is a generous ~25° rather than a few degrees.
"""

from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np

from fieldpilot.core.types import (
    KP_LEFT_EAR,
    KP_LEFT_EYE,
    KP_NOSE,
    KP_RIGHT_EAR,
    KP_RIGHT_EYE,
    PersonDetection,
    Perspective,
)

try:  # opencv is a core dependency, but safety logic must stay importable without it.
    import cv2
except ImportError:  # pragma: no cover - exercised only in numpy-only environments
    cv2 = None

# --- Generic 3D head model -----------------------------------------------------------------------
# Millimetres, origin at the nose tip, axes matching the camera frame for a head facing the camera
# (+X image right = the subject's own left, +Y down, +Z away from the camera). Values are generic
# adult anthropometry: ~95 mm between outer eye corners, ~155 mm head breadth at the ear canals,
# eyes ~65 mm behind the nose tip and ear canals ~130 mm behind it.
HEAD_MODEL_3D_MM: np.ndarray = np.array(
    [
        (0.0, 0.0, 0.0),          # KP_NOSE       - nose tip
        (47.5, -40.0, 65.0),      # KP_LEFT_EYE   - subject's left eye, outer corner
        (-47.5, -40.0, 65.0),     # KP_RIGHT_EYE  - subject's right eye, outer corner
        (77.5, -30.0, 130.0),     # KP_LEFT_EAR   - subject's left ear (tragus)
        (-77.5, -30.0, 130.0),    # KP_RIGHT_EAR  - subject's right ear (tragus)
    ],
    dtype=np.float64,
)
# Row order of HEAD_MODEL_3D_MM expressed as COCO-17 keypoint indices.
HEAD_MODEL_KP_ORDER: tuple[int, ...] = (
    KP_NOSE,
    KP_LEFT_EYE,
    KP_RIGHT_EYE,
    KP_LEFT_EAR,
    KP_RIGHT_EAR,
)

_MIN_PNP_POINTS = 4          # solvePnP needs >= 4 correspondences; the nose is always one of them.
_MIN_FACE_SPAN_PX = 10.0     # smaller faces make the fit numerically meaningless
_MAX_REPROJ_PX = 6.0         # absolute reprojection RMS budget
_MAX_REPROJ_FRAC = 0.25      # ...or this fraction of the face span, whichever is larger
_POSE_CACHE_MAX = 256        # bounded so long runs with many track ids cannot leak


class GazeEstimator:
    def __init__(self, cfg):
        self.mode = Perspective(str(cfg.get("app.perspective", "EXO")).upper())
        self.cone_deg = float(cfg.get("attention.gaze_cone_deg", 25))
        self.kp_conf = float(cfg.get("detection.keypoint_conf_min", 0.30))
        # frame size is learned from the first frame so the ego centre-cone can be scaled and an
        # uncalibrated pinhole camera matrix can be synthesised for solvePnP.
        self._frame_wh: tuple[int, int] | None = None
        # optional wearer orientation (yaw, pitch) in degrees, pushed from an IMU in EGO/BOTH mode.
        self._wearer_yaw_deg: float | None = None
        self._wearer_pitch_deg: float | None = None
        # real intrinsics, if a calibration was loaded; otherwise derived from the frame size.
        self._camera_matrix: np.ndarray | None = None
        self._dist_coeffs: np.ndarray | None = None
        # bounded LRU of the last pose per track id — feeds the GUI and memoises within a frame.
        self._poses: OrderedDict[int, dict] = OrderedDict()
        self._pose_sig: dict[int, tuple] = {}

    def set_frame_size(self, width: int, height: int) -> None:
        if self._frame_wh != (width, height):
            self._poses.clear()
            self._pose_sig.clear()
        self._frame_wh = (width, height)

    def set_orientation(self, yaw_deg: float, pitch_deg: float = 0.0) -> None:
        """Hook for the glasses/phone IMU stream (Milestone 2). Unused by the webcam proxy."""

        self._wearer_yaw_deg = yaw_deg
        self._wearer_pitch_deg = pitch_deg

    def set_intrinsics(self, camera_matrix, dist_coeffs=None) -> None:
        """Install real intrinsics; without them a pinhole guess from the frame size is used."""

        self._camera_matrix = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
        if dist_coeffs is None:
            self._dist_coeffs = np.zeros((5, 1), dtype=np.float64)
        else:
            self._dist_coeffs = np.asarray(dist_coeffs, dtype=np.float64).reshape(-1, 1)
        self._poses.clear()
        self._pose_sig.clear()

    def last_pose(self, track_id: int) -> dict | None:
        """Most recent head pose for a track: {yaw_deg, pitch_deg, roll_deg, method} or None."""

        pose = self._poses.get(int(track_id))
        return dict(pose) if pose is not None else None

    # ---- camera model ---------------------------------------------------------------------------
    def camera_matrix(self) -> np.ndarray | None:
        """Calibrated intrinsics if available, else a pinhole guess: f ≈ frame width, c = centre."""

        if self._camera_matrix is not None:
            return self._camera_matrix
        if self._frame_wh is None:
            return None
        w, h = self._frame_wh
        f = float(w)
        return np.array(
            [[f, 0.0, w / 2.0], [0.0, f, h / 2.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )

    def _dist(self) -> np.ndarray:
        if self._dist_coeffs is not None:
            return self._dist_coeffs
        return np.zeros((5, 1), dtype=np.float64)  # uncalibrated → assume no distortion

    # ---- head pose -----------------------------------------------------------------------------
    def _face_correspondences(self, person: PersonDetection):
        """Confident (3D mm, 2D px) facial pairs, nose first. None if PnP would be ill posed."""

        kps = person.keypoints
        if kps is None or len(kps) <= KP_RIGHT_EAR:
            return None
        if person.kp(KP_NOSE)[2] < self.kp_conf:
            return None  # without the nose tip there is no out-of-plane reference at all

        obj: list[tuple[float, float, float]] = []
        img: list[tuple[float, float]] = []
        for row, kp_idx in enumerate(HEAD_MODEL_KP_ORDER):
            x, y, c = person.kp(kp_idx)
            if c < self.kp_conf:
                continue
            obj.append(tuple(HEAD_MODEL_3D_MM[row]))
            img.append((x, y))
        if len(obj) < _MIN_PNP_POINTS:
            return None

        pts = np.asarray(img, dtype=np.float64)
        span = float(max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1])))
        if span < _MIN_FACE_SPAN_PX:
            return None  # a face a few pixels wide cannot support a 6-DoF fit
        return np.asarray(obj, dtype=np.float64), pts, span

    def _solve_head_pose(self, person: PersonDetection) -> dict | None:
        """cv2.solvePnP → Rodrigues → Euler yaw/pitch/roll in degrees, or None if unreliable."""

        if cv2 is None:
            return None
        cam = self.camera_matrix()
        if cam is None:
            return None
        pairs = self._face_correspondences(person)
        if pairs is None:
            return None
        obj, pts, span = pairs

        dist = self._dist()
        # SOLVEPNP_ITERATIVE (Levenberg-Marquardt) needs a seed with fewer than 6 points, so start
        # from a frontal head at the depth implied by the observed face width.
        f = float(cam[0, 0])
        z0 = max(200.0, f * 150.0 / max(span, 1.0))
        rvec = np.zeros((3, 1), dtype=np.float64)
        tvec = np.array([[0.0], [0.0], [z0]], dtype=np.float64)
        ok = False
        try:
            ok, rvec, tvec = cv2.solvePnP(
                obj, pts, cam, dist, rvec, tvec, True, cv2.SOLVEPNP_ITERATIVE
            )
        except cv2.error:
            ok = False
        if not ok:
            # Degenerate seeds / near-singular configurations: retry with a non-iterative solver.
            try:
                ok, rvec, tvec = cv2.solvePnP(obj, pts, cam, dist, flags=cv2.SOLVEPNP_SQPNP)
            except cv2.error:
                return None
        if not ok or float(tvec[2, 0]) <= 0.0:
            return None  # head behind the camera → nonsense fit

        # Reject fits that do not actually explain the observed keypoints.
        try:
            reproj, _ = cv2.projectPoints(obj, rvec, tvec, cam, dist)
        except cv2.error:
            return None
        rms = float(np.sqrt(np.mean(np.sum((reproj.reshape(-1, 2) - pts) ** 2, axis=1))))
        if not math.isfinite(rms) or rms > max(_MAX_REPROJ_PX, _MAX_REPROJ_FRAC * span):
            return None

        rot, _ = cv2.Rodrigues(rvec)
        yaw, pitch, roll = _euler_from_rotation(rot)
        if not all(math.isfinite(v) for v in (yaw, pitch, roll)):
            return None
        return {
            "yaw_deg": yaw,
            "pitch_deg": pitch,
            "roll_deg": roll,
            "yaw_rad": math.radians(yaw),
            "pitch_rad": math.radians(pitch),
            "roll_rad": math.radians(roll),
            "reproj_rms_px": rms,
            "n_points": int(len(obj)),
            "method": "solvepnp",
        }

    def _heuristic_pose(self, person: PersonDetection) -> dict | None:
        """M1 fallback: the nose's offset from the face midline encodes yaw. No pitch available."""

        nose = person.kp(KP_NOSE)
        if nose[2] < self.kp_conf:
            return None
        le, re = person.kp(KP_LEFT_EYE), person.kp(KP_RIGHT_EYE)
        lear, rear = person.kp(KP_LEFT_EAR), person.kp(KP_RIGHT_EAR)
        if le[2] >= self.kp_conf and re[2] >= self.kp_conf:
            mid_x, width = (le[0] + re[0]) / 2.0, abs(le[0] - re[0])
        elif lear[2] >= self.kp_conf and rear[2] >= self.kp_conf:
            mid_x, width = (lear[0] + rear[0]) / 2.0, abs(lear[0] - rear[0])
        else:
            return None
        if width < 1e-3:
            return None
        # nose right of the face midline → head turned toward the image right → positive yaw.
        yaw_ratio = (nose[0] - mid_x) / width           # roughly [-1, 1] over ~±70°
        yaw_deg = max(-70.0, min(70.0, yaw_ratio * 70.0))
        return {
            "yaw_deg": yaw_deg,
            "pitch_deg": 0.0,                            # unknowable from a single ratio
            "roll_deg": 0.0,
            "yaw_rad": math.radians(yaw_deg),
            "pitch_rad": 0.0,
            "roll_rad": 0.0,
            "method": "heuristic",
        }

    def head_pose(self, person: PersonDetection) -> dict | None:
        """Head pose for one person, solvePnP first and the M1 heuristic as a fallback.

        Result is cached per track id (bounded LRU) so repeated calls within a frame — the pipeline
        annotator asks again for every active hazard — do not re-run the solver.
        """

        tid = int(person.track_id)
        sig = _face_signature(person)
        if self._pose_sig.get(tid) == sig:
            cached = self._poses.get(tid)
            if cached is not None:
                self._poses.move_to_end(tid)
                return cached
            return None

        pose = self._solve_head_pose(person) or self._heuristic_pose(person)
        self._pose_sig[tid] = sig
        if pose is None:
            self._poses.pop(tid, None)
        else:
            self._poses[tid] = pose
            self._poses.move_to_end(tid)
        while len(self._poses) > _POSE_CACHE_MAX:
            old, _ = self._poses.popitem(last=False)
            self._pose_sig.pop(old, None)
        if len(self._pose_sig) > 4 * _POSE_CACHE_MAX:  # signatures for None poses have no LRU entry
            self._pose_sig = {k: v for k, v in self._pose_sig.items() if k in self._poses}
            self._pose_sig[tid] = sig
        return pose

    # ---- EXO: observed worker's head pose vs the bearing to the hazard --------------------------
    def _exo_looking_at(self, person: PersonDetection, bbox) -> bool:
        pose = self.head_pose(person)
        if pose is None:
            return False  # cannot tell → never claim the worker saw it

        origin = self._face_origin(person)
        if origin is None:
            return False
        bearing = self._bearing_deg(origin, bbox)
        if bearing is None:
            return False

        # the heuristic yaw is coarse and carries no pitch, so widen the cone on that path only.
        heuristic = pose["method"] == "heuristic"
        cone = self.cone_deg + (10.0 if heuristic else 0.0)
        d_yaw = _wrap_deg(bearing[0] - float(pose["yaw_deg"]))
        d_pitch = 0.0 if heuristic else _wrap_deg(bearing[1] - float(pose["pitch_deg"]))
        return math.hypot(d_yaw, d_pitch) <= cone

    def _face_origin(self, person: PersonDetection) -> tuple[float, float] | None:
        """Pixel the gaze ray leaves from: the nose tip, else the mean of confident face points."""

        nose = person.kp(KP_NOSE)
        if nose[2] >= self.kp_conf:
            return nose[0], nose[1]
        pts = [
            (x, y)
            for x, y, c in (person.kp(i) for i in HEAD_MODEL_KP_ORDER)
            if c >= self.kp_conf
        ]
        if not pts:
            return None
        return (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
        )

    def _bearing_deg(self, origin: tuple[float, float], bbox) -> tuple[float, float] | None:
        """Signed (horizontal, vertical) bearing in degrees from `origin` to the bbox centre.

        Angles are differences of camera rays, so they respect the lens geometry instead of treating
        pixels as degrees. Positive x = hazard to the image right; positive y = hazard above.
        """

        cam = self.camera_matrix()
        if cam is None:
            return None
        fx, fy = float(cam[0, 0]), float(cam[1, 1])
        px, py = float(cam[0, 2]), float(cam[1, 2])
        if fx <= 0.0 or fy <= 0.0:
            return None
        hx = (float(bbox[0]) + float(bbox[2])) / 2.0
        hy = (float(bbox[1]) + float(bbox[3])) / 2.0
        ang_h_x = math.degrees(math.atan2(hx - px, fx))
        ang_f_x = math.degrees(math.atan2(origin[0] - px, fx))
        ang_h_y = math.degrees(math.atan2(hy - py, fy))
        ang_f_y = math.degrees(math.atan2(origin[1] - py, fy))
        return ang_h_x - ang_f_x, ang_f_y - ang_h_y

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


def _euler_from_rotation(rot: np.ndarray) -> tuple[float, float, float]:
    """Decompose a camera-frame rotation matrix into (yaw, pitch, roll) degrees.

    The matrix is read as R = Rz(θz) · Ry(θy) · Rx(θx) about the camera axes (X right, Y down,
    Z forward). The returned values are re-signed to the module's public convention, i.e. they equal
    the head's facing direction expressed as bearings off the optical axis: yaw > 0 = turned toward
    the image right, pitch > 0 = tilted up, roll > 0 = clockwise in the image.
    """

    r = np.asarray(rot, dtype=np.float64)
    sin_y = -float(r[2, 0])
    cos_y = math.sqrt(max(0.0, float(r[0, 0]) ** 2 + float(r[1, 0]) ** 2))
    theta_y = math.asin(max(-1.0, min(1.0, sin_y)))
    if cos_y < 1e-6:  # gimbal lock: yaw ≈ ±90°, roll and pitch are not separable
        theta_x = math.atan2(-float(r[1, 2]), float(r[1, 1]))
        theta_z = 0.0
    else:
        theta_x = math.atan2(float(r[2, 1]), float(r[2, 2]))
        theta_z = math.atan2(float(r[1, 0]), float(r[0, 0]))
    return -math.degrees(theta_y), -math.degrees(theta_x), math.degrees(theta_z)


def rotation_from_euler(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """Inverse of `_euler_from_rotation` — the camera-frame rotation for the given public angles."""

    tx, ty, tz = math.radians(-pitch_deg), math.radians(-yaw_deg), math.radians(roll_deg)
    rx = np.array(
        [[1.0, 0.0, 0.0], [0.0, math.cos(tx), -math.sin(tx)], [0.0, math.sin(tx), math.cos(tx)]]
    )
    ry = np.array(
        [[math.cos(ty), 0.0, math.sin(ty)], [0.0, 1.0, 0.0], [-math.sin(ty), 0.0, math.cos(ty)]]
    )
    rz = np.array(
        [[math.cos(tz), -math.sin(tz), 0.0], [math.sin(tz), math.cos(tz), 0.0], [0.0, 0.0, 1.0]]
    )
    return rz @ ry @ rx


def _face_signature(person: PersonDetection) -> tuple:
    """Cheap identity for a person's face keypoints, used to memoise the solver within a frame."""

    return tuple(
        round(v, 2)
        for idx in HEAD_MODEL_KP_ORDER
        for v in person.kp(idx)
    )


def _wrap_deg(angle: float) -> float:
    """Wrap to [-180, 180]."""

    return (angle + 180.0) % 360.0 - 180.0
