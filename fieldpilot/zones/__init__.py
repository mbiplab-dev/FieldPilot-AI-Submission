"""Site zones — the relational spine the PRD assumes for zone-scoped behaviour.

A zone is the routing key for cross-worker broadcast, the `must`-filter key for blueprint
retrieval, and the source of the hazard level that rule conditions read. Before this module
`zone` was only an opaque string stamped onto events.
"""

from fieldpilot.zones.service import DEFAULT_ZONES, ZONES_TABLE, Zone, ZoneService

__all__ = ["DEFAULT_ZONES", "ZONES_TABLE", "Zone", "ZoneService"]
