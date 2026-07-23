"""PPE (hard-hat / hi-vis vest) checking via a dedicated detector.

YOLOv8-Pose only yields person keypoints — it does NOT detect PPE — so PPE uses a *separate* object
detector (default: a 10-class construction model with explicit Hardhat / NO-Hardhat / Safety Vest /
NO-Safety Vest classes). Violation boxes are associated to the nearest tracked worker and raised as
PPE_MISSING events; every PPE box is also exposed via `last_boxes` so the GUI can draw compliant
(green) and violation (red) detections on the live feed.

Pluggable: point `detection.ppe_model` at any YOLO PPE model, or set it null to disable cleanly.
"""

from __future__ import annotations

from fieldpilot.core.types import (
    FrameResult,
    HazardEvent,
    HazardType,
    PersonDetection,
    Severity,
)


class PPEChecker:
    def __init__(self, cfg):
        self.enabled = False
        self.cooldown_s = float(cfg.get("alerts.cooldown_s.ppe_missing", 20))
        self.conf_min = float(cfg.get("detection.conf_min", 0.35))
        self._model = None
        self._names: dict[int, str] = {}
        self._device = None
        self._last_alert: dict[tuple, float] = {}
        self.last_boxes: list[dict] = []       # {label, bbox, cat, ok} PPE boxes for the overlay
        self.equipment_boxes: list[dict] = []  # {label, bbox, kind} machinery/vehicle/cone
        model_path = cfg.get("detection.ppe_model")
        device = cfg.get("detection.device", "auto")
        if model_path:
            self._load(model_path, device)

    def _load(self, model_path: str, device: str) -> None:
        try:
            from ultralytics import YOLO

            self._model = YOLO(model_path)
            self._device = None if device == "auto" else device
            self._names = dict(self._model.names)
            self.enabled = True
        except Exception:  # noqa: BLE001 — PPE is optional; never take down the safety loop.
            self._model = None
            self.enabled = False

    @staticmethod
    def _categorize(name: str) -> tuple[str | None, bool]:
        """Map a class name to (category, is_compliant). category is 'helmet' | 'vest' | None."""

        nl = name.lower().replace(" ", "").replace("-", "").replace("_", "")
        if "hardhat" in nl or "helmet" in nl:
            cat = "helmet"
        elif "vest" in nl:
            cat = "vest"
        else:
            return None, True
        violation = nl.startswith("no")  # NO-Hardhat / NO-Safety-Vest / nohelmet
        return cat, not violation

    def _match_person(self, bbox, persons: list[PersonDetection]) -> int | None:
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        for p in persons:
            x1, y1, x2, y2 = p.bbox
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                return p.track_id
        return None

    def update(self, result: FrameResult) -> list[HazardEvent]:
        self.last_boxes = []
        self.equipment_boxes = []
        if not self.enabled or self._model is None:
            return []
        try:
            preds = self._model.predict(
                result.frame.image, conf=self.conf_min, device=self._device, verbose=False
            )
        except Exception:  # noqa: BLE001
            return []

        boxes: list[dict] = []
        equipment: list[dict] = []
        for r in preds:
            for b in r.boxes:
                name = self._names.get(int(b.cls), "")
                xyxy = tuple(float(v) for v in b.xyxy[0].tolist())
                cat, ok = self._categorize(name)
                if cat is not None:
                    boxes.append({"label": name, "bbox": xyxy, "cat": cat, "ok": ok})
                    continue
                nl = name.lower()
                if any(k in nl for k in ("machinery", "machine", "excavator", "loader", "crane",
                                         "bulldozer", "truck", "vehicle")):
                    kind = "vehicle" if ("vehicle" in nl or "truck" in nl) else "machinery"
                    equipment.append({"label": name, "bbox": xyxy, "kind": kind})
                elif "cone" in nl:
                    equipment.append({"label": name, "bbox": xyxy, "kind": "cone"})
        self.last_boxes = boxes
        self.equipment_boxes = equipment
        if not result.persons:
            return []

        events: list[HazardEvent] = []
        now = result.frame.ts_monotonic
        for box in boxes:
            if box["ok"]:
                continue
            tid = self._match_person(box["bbox"], result.persons)
            key = (tid, box["cat"])
            if now - self._last_alert.get(key, -1e9) < self.cooldown_s:
                continue
            self._last_alert[key] = now
            who = f"Worker {tid}" if tid is not None else "A worker"
            item = "hard hat" if box["cat"] == "helmet" else "safety vest"
            events.append(
                HazardEvent(
                    hazard_type=HazardType.PPE_MISSING,
                    severity=Severity.MEDIUM,
                    message=f"{who} is missing a {item}.",
                    frame_index=result.frame.index,
                    ts_monotonic=now,
                    track_id=tid,
                    bbox=box["bbox"],
                    meta={"ppe": box["cat"], "class": box["label"]},
                )
            )
        return events
