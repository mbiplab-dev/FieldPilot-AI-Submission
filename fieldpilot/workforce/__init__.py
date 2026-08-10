"""Workforce-facing concerns: where workers are, and what they ask.

`occupancy` tracks a worker checking in and out of a zone, and aggregates alerts per zone so a
site manager can see who is exposed and which zones are generating the most warnings.

`questions` is the worker's "ask about this, with a photo" flow — agents A5 (Voice/NLP) and A7
(Knowledge Retrieval) in docs/agents.md. It answers from the site's own specification corpus AND
routes the question to the site manager, because an LLM alone should not be the last word on a
safety question.

`messages` is the direct manager↔worker conversation — deliberately separate from `questions`,
which is a formal request with a lifecycle and an LLM answer. "On my way" does not belong on the
same review queue as an unanswered compliance question.

All of these keep the event bus out of their signatures so they stay unit-testable;
`backend/app.py` publishes on their behalf.
"""

from fieldpilot.workforce.messages import (
    MESSAGES_TABLE,
    SENDER_ROLES,
    MessageError,
    MessageService,
    resolve_audio_path,
)
from fieldpilot.workforce.occupancy import (
    OCCUPANCY_TABLE,
    OccupancyMismatchError,
    OccupancyService,
)
from fieldpilot.workforce.questions import (
    QUESTIONS_TABLE,
    STATUSES,
    QuestionError,
    QuestionService,
    resolve_image_path,
)

__all__ = [
    "MESSAGES_TABLE",
    "OCCUPANCY_TABLE",
    "QUESTIONS_TABLE",
    "SENDER_ROLES",
    "STATUSES",
    "MessageError",
    "MessageService",
    "OccupancyMismatchError",
    "OccupancyService",
    "QuestionError",
    "QuestionService",
    "resolve_audio_path",
    "resolve_image_path",
]
