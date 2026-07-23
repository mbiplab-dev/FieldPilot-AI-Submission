"""Proximity / danger-zone safety monitoring.

Implements the classic construction-safety use case (viso.ai): flag a worker who gets too close to
heavy machinery or a moving vehicle. Consumes the equipment boxes already produced by the PPE
detector's single inference pass (no extra model call) and the tracked worker boxes from pose.

Distance is the pixel gap between the worker and equipment rectangles (0 if overlapping), thresholded
as a fraction of the frame diagonal so it is scale-invariant.
"""

from __future__ import annotations

import math

from fieldpilot.core.types import FrameResult, HazardEvent, HazardType, Severity


def _rect_gap(a, b) -> float:
    """Shortest gap between two axis-aligned rectangles (0 if they overlap)."""

    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(0.0, max(bx1 - ax2, ax1 - bx2))
    dy = max(0.0, max(by1 - ay2, ay1 - by2))
    return math.hypot(dx, dy)


class ProximityDetector:
    def __init__(self, cfg):
        prox = cfg.section("proximity")
        self.enabled = bool(prox.get("enabled", True))
        self.danger_frac = float(prox.get("danger_distance_frac", 0.10))
        self.cooldown_s = float(cfg.get("alerts.cooldown_s.proximity", 8))
        self._last: dict[tuple, float] = {}

    def update(self, result: FrameResult, equipment_boxes: list[dict]) -> list[HazardEvent]:
        if not self.enabled or not equipment_boxes or not result.persons:
            return []
        hazards = [e for e in equipment_boxes if e["kind"] in ("machinery", "vehicle")]
        if not hazards:
            return []

        diag = math.hypot(result.frame.width, result.frame.height)
        thresh = self.danger_frac * diag
        now = result.frame.ts_monotonic
        events: list[HazardEvent] = []

        for person in result.persons:
            for eq in hazards:
                gap = _rect_gap(person.bbox, eq["bbox"])
                if gap > thresh:
                    continue
                key = (person.track_id, eq["kind"])
                if now - self._last.get(key, -1e9) < self.cooldown_s:
                    break
                self._last[key] = now
                events.append(
                    HazardEvent(
                        hazard_type=HazardType.PROXIMITY,
                        severity=Severity.HIGH,
                        message=f"Worker {person.track_id} is dangerously close to {eq['kind']}.",
                        frame_index=result.frame.index,
                        ts_monotonic=now,
                        track_id=person.track_id,
                        bbox=person.bbox,
                        meta={"equipment": eq["label"], "gap_px": round(gap, 1)},
                    )
                )
                break
        return events
