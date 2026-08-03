"""Canonical event schema — the single contract every AI model emits.

Every detector (PPE, fall, crack, measurement, fire, gas, RFI, proximity, …) publishes
this exact shape onto the event bus. Nothing else in the system talks to models directly;
the schema is deliberately transport-agnostic (JSON-serialisable) so the same event can
flow over Redis pub/sub, Kafka, or a plain HTTP POST.

Fields (per platform spec):
    event_id, worker_id, camera_id, zone, timestamp, event_type,
    confidence, severity, payload, image_url, video_url
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EventType(StrEnum):
    """All event families the bus supports."""

    PPE = "ppe"
    FALL = "fall"
    CRACK = "crack"
    INSPECTION = "inspection"
    MEASUREMENT = "measurement"
    FIRE = "fire"
    GAS = "gas"
    RFI = "rfi"
    PROXIMITY = "proximity"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_event_id() -> str:
    return uuid.uuid4().hex


class Event(BaseModel):
    """The canonical platform event emitted by every AI model."""

    event_id: str = Field(default_factory=new_event_id)
    worker_id: str | None = None
    camera_id: str = "cam-unknown"
    zone: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    event_type: EventType
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    severity: Severity = Severity.MEDIUM
    payload: dict[str, Any] = Field(default_factory=dict)
    image_url: str | None = None
    video_url: str | None = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_ts(cls, v):
        # accept epoch seconds for convenience from edge devices
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), UTC)
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    def dedup_key(self) -> str:
        """Stable identity used by the trigger engine to collapse duplicates.

        Two detections of "the same underlying issue" must share a key: same event type,
        same worker, same zone, and a model-supplied `dedup_key` payload discriminator
        (e.g. the missing PPE item, the crack track id, the measured element id).
        """

        # Producers should set `dedup_key` explicitly. When one forgets, fall back to the
        # natural subject of each event family rather than to an empty string — otherwise two
        # unrelated issues in the same zone (a rebar deviation and a slab deviation, or a
        # missing helmet and a missing vest) collapse into one alert and the second never
        # reaches the rules engine.
        discriminator = str(
            self.payload.get("dedup_key")
            or self.payload.get("subject")
            or self.payload.get("element")      # measurement events
            or self.payload.get("ppe_item")     # ppe events
            or self.payload.get("defect")       # crack / inspection events
            or ""
        )
        parts = [self.event_type.value, self.worker_id or "-", self.zone or "-", discriminator]
        return ":".join(parts)

    def model_dump_json_safe(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        return d
