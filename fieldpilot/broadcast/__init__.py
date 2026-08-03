"""Live push to browsers and worker devices, with zone-scoped cross-worker advisories.

Two jobs, one transport:

* **Dashboard liveness** — alerts, notifications, RFIs and learning progress are pushed to open
  browsers so the UI stops polling.
* **Cross-worker advisory** (PRD §4.4) — when a hazard fires for one worker, a *lower-priority*
  advisory goes to the other devices in the same zone only. Workers elsewhere on site are not
  interrupted.

Fan-out rides the existing event bus, so a Redis deployment reaches every backend replica and the
in-memory backend keeps tests infrastructure-free.
"""

from fieldpilot.broadcast.hub import BROADCAST_PATTERN, BroadcastHub, Client

__all__ = ["BROADCAST_PATTERN", "BroadcastHub", "Client"]
