"""Feedback-driven fine-tuning with an honest mAP50 gate.

`docs/plan.md` correction #5: no "guaranteed mAP gain". This package measures the delta on a locked
validation set and promotes weights only when the delta clears the configured floor; regressions
are recorded as completed runs that were not promoted.
"""

from fieldpilot.learning.audit import DatasetAudit, audit_yolo_dataset
from fieldpilot.learning.captures import (
    CAPTURE_FRAMES_TABLE,
    CAPTURE_SESSIONS_TABLE,
    PPE_CLASSES,
    CaptureError,
    CaptureService,
)
from fieldpilot.learning.dataset import DatasetSummary, build_dataset
from fieldpilot.learning.service import LEARNING_RUNS_TABLE, LearningService

__all__ = [
    "LEARNING_RUNS_TABLE",
    "CAPTURE_FRAMES_TABLE",
    "CAPTURE_SESSIONS_TABLE",
    "PPE_CLASSES",
    "CaptureError",
    "CaptureService",
    "DatasetAudit",
    "DatasetSummary",
    "LearningService",
    "audit_yolo_dataset",
    "build_dataset",
]
