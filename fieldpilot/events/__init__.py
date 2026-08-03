"""Event-driven core: canonical event schema, event bus, and durable event storage.

The one architectural rule of the platform:

    Model → Event → Rules Engine → Notification → Dashboard

AI models NEVER call notification or dashboard APIs directly. They publish an `Event`
onto the `EventBus`. The trigger engine filters/merges them, the rules engine decides
what is actionable, and only then are notifications and dashboard updates produced.
"""

from fieldpilot.events.schema import Event, EventType, Severity

__all__ = ["Event", "EventType", "Severity"]
