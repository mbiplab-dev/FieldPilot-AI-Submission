"""Workforce-facing concerns: where workers are, and what they ask.

`occupancy` tracks a worker checking in and out of a zone, and aggregates alerts per zone so a
site manager can see who is exposed and which zones are generating the most warnings.

`questions` (NOT YET BUILT — see BUILD_LOG.txt §5) is the worker's "ask about this, with a photo"
flow: agent A5 (Voice/NLP) and A7 (Knowledge Retrieval) in docs/agents.md.

Both keep the event bus out of their signatures so they stay unit-testable; `backend/app.py`
publishes on their behalf.
"""

from fieldpilot.workforce.occupancy import (
    OCCUPANCY_TABLE,
    OccupancyMismatchError,
    OccupancyService,
)

__all__ = [
    "OCCUPANCY_TABLE",
    "OccupancyMismatchError",
    "OccupancyService",
]
