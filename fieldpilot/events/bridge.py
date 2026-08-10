"""Bridge from the edge pipeline to the platform event bus.

The existing detectors (PPE, fall, proximity, attention) emit `HazardEvent`s. The bridge
converts each into the canonical `Event` and publishes it onto the bus — this is how the
models "publish events instead of calling APIs directly". The old direct-to-dispatcher
path is replaced by a bus subscription downstream.
"""

from __future__ import annotations

from fieldpilot.core.types import HazardEvent, HazardType
from fieldpilot.core.types import Severity as EdgeSeverity
from fieldpilot.events.bus import EventBus, publish_event
from fieldpilot.events.schema import Event, EventType, Severity

_HAZARD_TO_EVENT: dict[HazardType, EventType] = {
    HazardType.FALL: EventType.FALL,
    HazardType.PPE_MISSING: EventType.PPE,
    HazardType.PROXIMITY: EventType.PROXIMITY,
    HazardType.UNNOTICED_HAZARD: EventType.PPE,  # attention escalations ride the ppe lane
    HazardType.CRACK: EventType.CRACK,
}

_SEVERITY_MAP = {
    EdgeSeverity.LOW: Severity.LOW,
    EdgeSeverity.MEDIUM: Severity.MEDIUM,
    EdgeSeverity.HIGH: Severity.HIGH,
}


def hazard_to_event(
    hazard: HazardEvent,
    *,
    camera_id: str = "cam-edge-0",
    zone: str | None = None,
) -> Event:
    etype = _HAZARD_TO_EVENT.get(hazard.hazard_type, EventType.PPE)
    payload = dict(hazard.meta)
    payload["message"] = hazard.message
    payload["frame_index"] = hazard.frame_index
    # stable discriminator so the trigger engine can collapse duplicates of the same issue
    payload.setdefault("dedup_key", str(hazard.meta.get("ppe") or hazard.hazard_type.value))
    if hazard.bbox is not None:
        payload["bbox"] = list(hazard.bbox)

    # The ingest path may know the origin better than the bridge's construction-time defaults.
    # A phone streaming into the shared browser pipeline is the case that needs this: one bridge
    # serves every connected device, so "which phone, in which zone" can only travel with the
    # hazard itself. Absent these keys nothing changes.
    source_camera = hazard.meta.get("source_camera_id")
    source_zone = hazard.meta.get("source_zone")

    return Event(
        worker_id=f"w-{hazard.track_id}" if hazard.track_id is not None else None,
        camera_id=str(source_camera) if source_camera else camera_id,
        zone=str(source_zone) if source_zone else zone,
        timestamp=hazard.ts_wall,
        event_type=etype,
        confidence=float(hazard.meta.get("confidence", 1.0)),
        severity=_SEVERITY_MAP.get(hazard.severity, Severity.MEDIUM),
        payload=payload,
        image_url=hazard.meta.get("image_url"),
    )


class PipelineEventBridge:
    """Sink used by the edge `Pipeline`: HazardEvent in, canonical Event out."""

    def __init__(self, bus: EventBus, *, camera_id: str = "cam-edge-0", zone: str | None = None):
        self.bus = bus
        self.camera_id = camera_id
        self.zone = zone

    async def emit(self, hazard: HazardEvent) -> Event:
        event = hazard_to_event(hazard, camera_id=self.camera_id, zone=self.zone)
        await publish_event(self.bus, event)
        return event
