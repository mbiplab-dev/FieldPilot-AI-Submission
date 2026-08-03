"""Intelligent Trigger Engine — the filtering layer between raw model events and alerts.

Without it, a PPE model generates ~500 alerts/minute. With it:

- duplicates of the same underlying issue inside a 45 s window are ignored (SUPPRESSED),
- repeated detections are merged into one alert with a hit counter,
- alerts are tracked while the issue persists (NEW → ACTIVE),
- alerts auto-resolve when the issue disappears (RESOLVED),
- operators can suppress noise sources (SUPPRESSED).
"""

from fieldpilot.triggers.engine import Alert, AlertState, ProcessResult, TriggerEngine

__all__ = ["Alert", "AlertState", "ProcessResult", "TriggerEngine"]
