"""Feedback-driven fine-tuning with an honest mAP50 gate.

`plan.md` correction #5: no "guaranteed mAP gain". This package measures the delta on a locked
validation set and promotes weights only when the delta clears the configured floor; regressions
are recorded as completed runs that were not promoted.
"""

from fieldpilot.learning.dataset import DatasetSummary, build_dataset
from fieldpilot.learning.service import LEARNING_RUNS_TABLE, LearningService

__all__ = ["LEARNING_RUNS_TABLE", "DatasetSummary", "LearningService", "build_dataset"]
