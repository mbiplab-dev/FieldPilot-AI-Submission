"""Supervisor feedback — the closed loop the PRD's accuracy claim depends on.

A supervisor approves or rejects an alert on the dashboard; the decision is stored together with
the frame that produced it and the bounding box that was drawn, which is exactly the shape the
learning loop needs to build a supervised dataset. Rows stay `consumed_at IS NULL` until a
training run claims them, so every sample is used once and runs are reproducible.
"""

from fieldpilot.feedback.service import (
    FEEDBACK_TABLE,
    FeedbackDecision,
    FeedbackService,
)

__all__ = ["FEEDBACK_TABLE", "FeedbackDecision", "FeedbackService"]
