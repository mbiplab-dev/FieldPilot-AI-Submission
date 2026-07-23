"""Vision engine: YOLOv8-Pose + BoT-SORT.

Wraps the Ultralytics API. Tracking uses `persist=True` so BoT-SORT keeps stable worker IDs across
frames (and short occlusions). `track_buffer` and `match_thresh` are surfaced from `config.yaml` and
injected into a generated tracker config so they can be tuned against ID flickering without editing
package files. Heavy imports (torch/ultralytics) are confined to this module.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import yaml

from fieldpilot.core.types import NUM_KEYPOINTS, Frame, FrameResult, PersonDetection
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.vision")


def _build_tracker_cfg(cfg) -> str:
    """Clone the shipped BoT-SORT config and override the two tuning knobs; return its path.

    Cloning the packaged default keeps us compatible with whatever fields the installed Ultralytics
    version expects, instead of hand-writing a YAML that may drift out of date.
    """

    track_buffer = int(cfg.get("detection.track_buffer", 30))
    match_thresh = float(cfg.get("detection.match_thresh", 0.8))
    try:
        import ultralytics

        default = Path(ultralytics.__file__).parent / "cfg" / "trackers" / "botsort.yaml"
        data = yaml.safe_load(default.read_text())
        data["track_buffer"] = track_buffer
        data["match_thresh"] = match_thresh
        out = Path("models") / "botsort_fieldpilot.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(data, sort_keys=False))
        return str(out)
    except Exception:  # noqa: BLE001
        log.warning("using built-in botsort.yaml (could not clone default for tuning)", exc_info=True)
        return "botsort.yaml"


class VisionEngine:
    def __init__(self, cfg):
        from ultralytics import YOLO

        det = cfg.section("detection")
        self.conf_min = float(det.get("conf_min", 0.35))
        self.iou = float(det.get("iou", 0.5))
        self.imgsz = int(cfg.get("video.infer_size", 640))
        device = str(det.get("device", "auto"))
        self.device = None if device == "auto" else device

        model_path = det.get("pose_model", "models/yolov8n-pose.pt")
        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        # Ultralytics auto-downloads the weights on first construction if the file is absent.
        self.model = YOLO(model_path)
        self.tracker_cfg = _build_tracker_cfg(cfg)
        log.info("VisionEngine ready: model=%s imgsz=%d device=%s tracker=%s",
                 model_path, self.imgsz, self.device or "auto", self.tracker_cfg)

    def infer(self, frame: Frame) -> FrameResult:
        t0 = time.monotonic()
        results = self.model.track(
            source=frame.image,
            persist=True,
            tracker=self.tracker_cfg,
            conf=self.conf_min,
            iou=self.iou,
            imgsz=self.imgsz,
            device=self.device,
            verbose=False,
        )
        infer_ms = (time.monotonic() - t0) * 1000.0
        persons = self._parse(results)
        return FrameResult(frame=frame, persons=persons, infer_ms=infer_ms)

    def _parse(self, results) -> list[PersonDetection]:
        persons: list[PersonDetection] = []
        if not results:
            return persons
        r = results[0]
        if r.boxes is None or len(r.boxes) == 0:
            return persons

        xyxy = r.boxes.xyxy.cpu().numpy()
        confs = r.boxes.conf.cpu().numpy()
        ids = r.boxes.id.cpu().numpy().astype(int) if r.boxes.id is not None else None
        if r.keypoints is not None and r.keypoints.data is not None:
            kpts = r.keypoints.data.cpu().numpy()  # (N, 17, 3)
        else:
            kpts = np.zeros((len(xyxy), NUM_KEYPOINTS, 3), dtype=np.float32)

        for i in range(len(xyxy)):
            track_id = int(ids[i]) if ids is not None else -(i + 1)  # stable id, or per-frame fallback
            kp = kpts[i] if i < len(kpts) else np.zeros((NUM_KEYPOINTS, 3), dtype=np.float32)
            persons.append(
                PersonDetection(
                    track_id=track_id,
                    bbox=tuple(float(v) for v in xyxy[i]),
                    conf=float(confs[i]),
                    keypoints=kp.astype(np.float32),
                )
            )
        return persons
