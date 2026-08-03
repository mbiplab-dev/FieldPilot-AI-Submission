"""Shared data types for the FieldPilot pipeline.

Deliberately dependency-light (numpy only) so safety logic and its unit tests can run without the
heavy CV/ML stack (torch/ultralytics) installed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

# COCO-17 keypoint order emitted by YOLOv8-Pose.
KP_NOSE = 0
KP_LEFT_EYE = 1
KP_RIGHT_EYE = 2
KP_LEFT_EAR = 3
KP_RIGHT_EAR = 4
KP_LEFT_SHOULDER = 5
KP_RIGHT_SHOULDER = 6
KP_LEFT_ELBOW = 7
KP_RIGHT_ELBOW = 8
KP_LEFT_WRIST = 9
KP_RIGHT_WRIST = 10
KP_LEFT_HIP = 11
KP_RIGHT_HIP = 12
KP_LEFT_KNEE = 13
KP_RIGHT_KNEE = 14
KP_LEFT_ANKLE = 15
KP_RIGHT_ANKLE = 16
NUM_KEYPOINTS = 17

FACE_KEYPOINTS = (KP_NOSE, KP_LEFT_EYE, KP_RIGHT_EYE, KP_LEFT_EAR, KP_RIGHT_EAR)


class Perspective(StrEnum):
    EGO = "EGO"    # wearer's own state (IMU / device orientation)
    EXO = "EXO"    # workers observed in the camera view
    BOTH = "BOTH"


class HazardType(StrEnum):
    FALL = "fall"
    PPE_MISSING = "ppe_missing"
    UNNOTICED_HAZARD = "unnoticed_hazard"
    PROXIMITY = "proximity"
    CRACK = "crack"  # structural defect from the inspection detector


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AttentionState(StrEnum):
    PASSIVE = "passive"      # no active hazard being tracked for this worker
    NOTICED = "noticed"      # worker dwelled on the hazard long enough (cognitive engagement)
    UNNOTICED = "unnoticed"  # hazard present, worker has not engaged
    ESCALATED = "escalated"  # unnoticed too long -> escalate


@dataclass
class Frame:
    """A single captured frame moving through the pipeline."""

    index: int
    ts_monotonic: float           # time.monotonic() at capture — used for latency accounting
    image: np.ndarray             # BGR HxWx3 as delivered by OpenCV
    ts_wall: float = field(default_factory=time.time)

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def width(self) -> int:
        return int(self.image.shape[1])


@dataclass
class PersonDetection:
    """One tracked person in a frame."""

    track_id: int
    bbox: tuple[float, float, float, float]   # x1, y1, x2, y2 (pixels)
    conf: float
    # (17, 3) array of [x, y, confidence]; x/y in pixels, confidence in [0, 1].
    keypoints: np.ndarray

    def kp(self, idx: int) -> tuple[float, float, float]:
        x, y, c = self.keypoints[idx]
        return float(x), float(y), float(c)

    def kp_visible(self, idx: int, min_conf: float) -> bool:
        return bool(self.keypoints[idx, 2] >= min_conf)

    @property
    def bbox_center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    @property
    def bbox_height(self) -> float:
        _, y1, _, y2 = self.bbox
        return y2 - y1


@dataclass
class FrameResult:
    """Detection output for one frame."""

    frame: Frame
    persons: list[PersonDetection]
    infer_ms: float = 0.0


@dataclass
class HazardEvent:
    """A hazard emitted by a safety detector; the unit the alert + logging + learning act on."""

    hazard_type: HazardType
    severity: Severity
    message: str
    frame_index: int
    ts_monotonic: float
    track_id: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    meta: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts_wall: float = field(default_factory=time.time)

    def category(self) -> str:
        return self.hazard_type.value
