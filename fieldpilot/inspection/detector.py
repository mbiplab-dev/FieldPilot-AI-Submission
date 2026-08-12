"""Structural-damage inspection detector.

Wraps the fine-tuned YOLO damage model and emits `crack` hazard events with a
`severity_score` that the platform rules engine acts on. Designed to be cheap:
it runs every `frame_skip` frames and only while inspection mode is enabled.

Class → severity mapping (calibrated so severe damage trips the > 0.85 rule):
    Minorrotation    → 0.45 (monitor)
    Moderaterotation → 0.70 (inspect soon)
    Severerotation   → 0.92 (immediate inspection)

Deduplication is delegated to the trigger engine: each detection carries a
`dedup_key` of `{class}:{coarse-grid-cell}` so the same physical defect merges
into one alert while defects in different locations stay separate.
"""

from __future__ import annotations

from fieldpilot.core.types import (
    FrameResult,
    HazardEvent,
    HazardType,
    Severity,
)
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.inspection")

# class name (lowercased) → (severity_score, edge severity)
_CLASS_SEVERITY: dict[str, tuple[float, Severity]] = {
    "minorrotation": (0.45, Severity.LOW),
    "moderaterotation": (0.70, Severity.MEDIUM),
    "severerotation": (0.92, Severity.HIGH),
}
_DEFAULT_SEVERITY = (0.50, Severity.MEDIUM)
_GRID_PX = 96  # coarse spatial cell for dedup (same physical defect → same cell)


class InspectionDetector:
    def __init__(self, cfg) -> None:
        ins = cfg.section("inspection")
        self.enabled = bool(ins.get("enabled", False))  # runtime-toggleable
        self.conf_min = float(ins.get("conf_min", 0.40))
        self.frame_skip = max(1, int(ins.get("frame_skip", 5)))
        self.available = False
        self._model = None
        self._names: dict[int, str] = {}
        self._device = None
        self._frame_idx = 0
        self.last_boxes: list[dict] = []  # {label, bbox, severity_score} for the overlay
        model_path = ins.get("model", "models/structural_damage_best.pt")
        if model_path:
            self._load(model_path, cfg.get("detection.device", "auto"))

    def _load(self, model_path: str, device: str) -> None:
        try:
            from ultralytics import YOLO

            self._model = YOLO(model_path)
            self._device = None if device == "auto" else device
            self._names = dict(self._model.names)
            self.available = True
            log.info("inspection model loaded: %s (classes: %s)", model_path, list(self._names.values()))
        except Exception:  # noqa: BLE001 — inspection is optional; never kill the loop
            log.warning("inspection model unavailable (%s) — inspection mode disabled", model_path)
            self._model = None
            self.available = False
            # Preserve the operator's requested state. `update()` is already guarded by a missing
            # model and `set_enabled()` refuses an unavailable one; mutating the request here made
            # a later hot-loaded/test model remain silently disabled even after it became ready.

    def set_enabled(self, enabled: bool) -> bool:
        """Toggle inspection mode. Returns the ACTUAL state (False if model missing)."""

        if enabled and not self.available:
            log.warning("inspection mode requested but model is not available")
            return False
        self.enabled = bool(enabled)
        if not self.enabled:
            self.last_boxes = []
        log.info("inspection mode %s", "ON" if self.enabled else "OFF")
        return self.enabled

    @staticmethod
    def _class_of(name: str) -> tuple[float, Severity, str]:
        key = name.lower().replace(" ", "").replace("-", "").replace("_", "")
        score, sev = _CLASS_SEVERITY.get(key, _DEFAULT_SEVERITY)
        return score, sev, key

    def update(self, result: FrameResult) -> list[HazardEvent]:
        if not self.enabled or self._model is None:
            return []
        self._frame_idx += 1
        if self._frame_idx % self.frame_skip != 0:
            return []  # keep last_boxes for overlay continuity between runs

        try:
            preds = self._model.predict(
                result.frame.image, conf=self.conf_min, device=self._device, verbose=False
            )
        except Exception:  # noqa: BLE001
            return []

        boxes: list[dict] = []
        events: list[HazardEvent] = []
        now = result.frame.ts_monotonic
        # Read names from the active model as well as the load-time cache. This keeps runtime model
        # swaps honest: a replacement checkpoint may have a different label map.
        names = dict(getattr(self._model, "names", None) or self._names)
        for r in preds:
            for b in r.boxes:
                name = names.get(int(b.cls), "defect")
                raw = b.xyxy[0]
                seq = raw.tolist() if hasattr(raw, "tolist") else raw
                xyxy = tuple(float(v) for v in seq)
                conf = float(b.conf[0]) if hasattr(b, "conf") else 1.0
                score, sev, cls_key = self._class_of(name)
                boxes.append({"label": f"{name} {score:.2f}", "bbox": xyxy,
                              "severity_score": score, "cls": cls_key})

                cx, cy = (xyxy[0] + xyxy[2]) / 2.0, (xyxy[1] + xyxy[3]) / 2.0
                cell = f"{int(cx // _GRID_PX)}:{int(cy // _GRID_PX)}"
                events.append(
                    HazardEvent(
                        hazard_type=HazardType.CRACK,
                        severity=sev,
                        message=f"Structural defect detected: {name} (severity {score:.2f}).",
                        frame_index=result.frame.index,
                        ts_monotonic=now,
                        track_id=None,
                        bbox=xyxy,
                        meta={
                            "defect": name,
                            "severity_score": score,
                            "confidence": round(conf, 3),
                            "dedup_key": f"{cls_key}:{cell}",
                        },
                    )
                )
        self.last_boxes = boxes
        return events
