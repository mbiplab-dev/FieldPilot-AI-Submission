"""The Tier-1 edge safety pipeline.

Wires capture → inference (YOLOv8-Pose + BoT-SORT) → safety detectors (fall, PPE, attention) →
multimodal alerts → durable SQLite log, as one async loop. Inference runs in a thread executor so
GPU work never blocks frame capture. Every admitted alert's detection→alert latency is recorded for
the <500 ms budget check.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from statistics import median

from fieldpilot.alerts.dispatcher import AlertDispatcher
from fieldpilot.core.config import Config
from fieldpilot.core.types import HazardEvent, HazardType
from fieldpilot.core.video_source import VideoSource
from fieldpilot.core.vision_engine import VisionEngine
from fieldpilot.inspection.detector import InspectionDetector
from fieldpilot.logging_.logger import get_logger, jsonl_append
from fieldpilot.logging_.store import EventStore
from fieldpilot.perspective import GazeEstimator
from fieldpilot.safety.attention import AttentionTracker
from fieldpilot.safety.fall import FallDetector, torso_metrics
from fieldpilot.safety.ppe import PPEChecker
from fieldpilot.safety.proximity import ProximityDetector

log = get_logger("fieldpilot.pipeline")

# hazards that require a worker to acknowledge them (fed to the attention tracker).
_ACK_REQUIRED = (HazardType.FALL, HazardType.PPE_MISSING, HazardType.PROXIMITY)

# COCO-17 skeleton edges for the on-screen overlay.
_SKELETON = [
    (5, 7), (7, 9), (6, 8), (8, 10),          # arms
    (5, 6), (5, 11), (6, 12), (11, 12),       # shoulders / torso / hips
    (11, 13), (13, 15), (12, 14), (14, 16),   # legs
    (0, 1), (0, 2), (1, 3), (2, 4),           # face
    (0, 5), (0, 6),                           # neck
]


class ActiveHazardRegistry:
    """Keeps primary hazards 'active' for a TTL so attention can be judged over time, not one frame."""

    def __init__(self, ttl_s: float):
        self.ttl = ttl_s
        self._store: dict[tuple, tuple[HazardEvent, float]] = {}

    def add(self, event: HazardEvent) -> None:
        key = (event.hazard_type, event.track_id)
        # keep the original event object (stable id) but refresh its expiry.
        existing = self._store.get(key)
        base = existing[0] if existing else event
        self._store[key] = (base, event.ts_monotonic + self.ttl)

    def active(self, now: float) -> list[HazardEvent]:
        out = []
        for key, (event, expires) in list(self._store.items()):
            if now <= expires:
                out.append(event)
            else:
                del self._store[key]
        return out


class Pipeline:
    def __init__(self, cfg: Config, *, show: bool = False, sink=None, event_bridge=None):
        self.cfg = cfg
        self.show = show
        self.sink = sink  # optional LiveState for the web GUI
        # when set, HazardEvents are published onto the platform event bus (via the bridge)
        # instead of going straight to the dispatcher — models never call APIs directly.
        self.event_bridge = event_bridge
        self.store = EventStore(cfg.get("storage.sqlite_path", "data/fieldpilot.db"))
        self.engine = VisionEngine(cfg)
        self.fall = FallDetector(cfg)
        self.ppe = PPEChecker(cfg)
        self.inspection = InspectionDetector(cfg)
        self.proximity = ProximityDetector(cfg)
        self.attention = AttentionTracker(cfg)
        self.gaze = GazeEstimator(cfg)
        self.dispatcher = AlertDispatcher(cfg)
        self.registry = ActiveHazardRegistry(float(cfg.get("attention.hazard_ttl_s", 8)))
        self.jsonl_path = cfg.get("logging.json_file", "data/events.log.jsonl")
        self.kp_conf = float(cfg.get("detection.keypoint_conf_min", 0.30))
        self.latencies: list[float] = []
        self.infer_ms: list[float] = []
        self.frame_count = 0
        self.hazard_count = 0
        self.frames_with_person = 0
        self.max_persons = 0
        self._track_ids: set[int] = set()
        self._type_counts: dict[str, int] = {}
        self._active: list[HazardEvent] = []
        self._last_persons = 0
        self._fps_times: deque[float] = deque(maxlen=30)
        self._quit = False
        self._frame_size_set = False

    def set_inspection(self, enabled: bool) -> bool:
        """Toggle inspection mode (called from the bus control channel). Returns actual."""

        return self.inspection.set_enabled(enabled)

    _ALERT_SHOTS_DIR = "data/alerts"
    _SEV_COLOR = {"high": (60, 60, 235), "medium": (40, 200, 235), "low": (80, 220, 80)}

    def _save_alert_image(self, event, frame_img) -> None:
        """Snapshot the flagged region (bbox + label) for the LLM + dashboard card."""

        if self.event_bridge is None:
            return
        import cv2  # noqa: PLC0415

        try:
            import os

            os.makedirs(self._ALERT_SHOTS_DIR, exist_ok=True)
            out = frame_img.copy()
            color = self._SEV_COLOR.get(event.severity.value, (60, 60, 235))
            if event.bbox is not None:
                x1, y1, x2, y2 = (int(v) for v in event.bbox)
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(out.shape[1], x2), min(out.shape[0], y2)
                cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
                _label(out, f"{event.hazard_type.value}", (x1, max(14, y1 - 6)), color)
            # light HUD so the snapshot is self-describing even without the bbox
            cv2.putText(out, f"{event.hazard_type.value} · {event.severity.value}",
                        (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 1, cv2.LINE_AA)
            ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                fname = f"{event.id}.jpg"
                (Path(self._ALERT_SHOTS_DIR) / fname).write_bytes(buf.tobytes())
                event.meta["image_url"] = f"/img/{fname}"
        except Exception:  # noqa: BLE001 — image capture is best-effort, never fatal
            pass

    def _process(self, result) -> list[HazardEvent]:
        primary: list[HazardEvent] = []
        primary.extend(self.fall.update(result))
        primary.extend(self.ppe.update(result))  # also fills self.ppe.equipment_boxes
        primary.extend(self.inspection.update(result))
        primary.extend(self.proximity.update(result, self.ppe.equipment_boxes))
        for ev in primary:
            if ev.hazard_type in _ACK_REQUIRED:
                self.registry.add(ev)
        self._active = self.registry.active(result.frame.ts_monotonic)
        attention_events = self.attention.observe(result, self._active, self.gaze.looking_at)
        return primary + attention_events

    async def run(self, source: VideoSource, *, max_seconds: float | None = None) -> dict:
        source.start()
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        log.info("pipeline started (perspective=%s, max_seconds=%s)",
                 self.cfg.get("app.perspective"), max_seconds)
        try:
            async for frame in source.frames():
                if not self._frame_size_set:
                    self.gaze.set_frame_size(frame.width, frame.height)
                    self._frame_size_set = True

                result = await loop.run_in_executor(None, self.engine.infer, frame)
                self.infer_ms.append(result.infer_ms)
                self._fps_times.append(time.monotonic())
                self._last_persons = len(result.persons)
                if result.persons:
                    self.frames_with_person += 1
                    self.max_persons = max(self.max_persons, len(result.persons))
                    self._track_ids.update(p.track_id for p in result.persons)

                for event in self._process(result):
                    self.hazard_count += 1
                    self._type_counts[event.category()] = self._type_counts.get(event.category(), 0) + 1
                    self.store.record_event(event, alerted=True)
                    if self.event_bridge is not None:
                        # event-driven mode: capture an annotated snapshot of the flagged
                        # region for the LLM verifier + the dashboard alert card, then publish.
                        self._save_alert_image(event, result.frame.image)
                        # event-driven mode: publish to the bus; alerting/notifications are
                        # downstream consumers (trigger engine → rules → notification service).
                        await self.event_bridge.emit(event)
                        record = None
                    else:
                        # offline M1 mode: direct local earcon/TTS/haptic dispatch.
                        record = self.dispatcher.dispatch(event)
                    jsonl_append(self.jsonl_path, {
                        "event": event,
                        "admitted": record.admitted if record else None,
                        "latency_ms": round(record.latency_ms, 1) if record else None,
                        "routed": "bus" if self.event_bridge is not None else "dispatcher",
                    })
                    if record is not None and record.admitted:
                        self.latencies.append(record.latency_ms)
                    if self.sink is not None:
                        self.sink.add_event({
                            "type": event.category(),
                            "severity": event.severity.value,
                            "message": event.message,
                            "track_id": event.track_id,
                            "latency_ms": (round(record.latency_ms, 1)
                                           if record is not None and record.admitted else None),
                            "ts_wall": event.ts_wall,
                        })

                self.frame_count += 1
                live = self._live_stats()
                if self.sink is not None:
                    annotated = annotate(result, self._active, self.gaze, self.kp_conf, live,
                                         self.ppe.last_boxes, self.fall.risk, self.ppe.equipment_boxes,
                                         self.inspection.last_boxes)
                    self.sink.update_frame(encode_jpeg(annotated), live)
                if self.show:
                    self._preview(result, live)
                    if self._quit:
                        break
                if max_seconds is not None and time.monotonic() - start >= max_seconds:
                    break
        finally:
            source.stop()
            self.dispatcher.shutdown()
            if self.sink is not None:
                self.sink.running = False
            if self.show:
                try:
                    import cv2

                    cv2.destroyAllWindows()
                except Exception:  # noqa: BLE001
                    pass

        return self.summary(elapsed=time.monotonic() - start, dropped=source.dropped)

    def _live_stats(self) -> dict:
        ft = self._fps_times
        fps = (len(ft) - 1) / (ft[-1] - ft[0]) if len(ft) >= 2 and ft[-1] > ft[0] else 0.0
        recent = self.infer_ms[-30:]
        return {
            "perspective": str(self.cfg.get("app.perspective")),
            "fps": round(fps, 1),
            "infer_ms": round(median(recent), 1) if recent else None,
            "frames": self.frame_count,
            "persons": self._last_persons,
            "unique_tracks": len(self._track_ids),
            "hazards": self.hazard_count,
            "alerts": len(self.latencies),
            "last_latency_ms": round(self.latencies[-1], 1) if self.latencies else None,
            "counts_by_type": dict(self._type_counts),
            "active_hazards": [
                {"type": h.hazard_type.value, "track_id": h.track_id} for h in self._active
            ],
        }

    def _preview(self, result, live: dict) -> None:
        try:
            import cv2

            img = annotate(result, self._active, self.gaze, self.kp_conf, live,
                           self.ppe.last_boxes, self.fall.risk, self.ppe.equipment_boxes,
                           self.inspection.last_boxes)
            cv2.imshow("FieldPilot AI", img)
            if (cv2.waitKey(1) & 0xFF) == ord("q"):
                self._quit = True
        except Exception:  # noqa: BLE001 — preview is best-effort (headless machines have no GUI).
            self.show = False

    def summary(self, elapsed: float, dropped: int = 0) -> dict:
        fps = self.frame_count / elapsed if elapsed > 0 else 0.0
        s = {
            "frames": self.frame_count,
            "elapsed_s": round(elapsed, 1),
            "fps": round(fps, 1),
            "dropped_frames": dropped,
            "frames_with_person": self.frames_with_person,
            "max_persons_in_frame": self.max_persons,
            "unique_track_ids": len(self._track_ids),
            "hazards": self.hazard_count,
            "alerts": len(self.latencies),
            "infer_ms_median": round(median(self.infer_ms), 1) if self.infer_ms else None,
            "latency_ms_median": round(median(self.latencies), 1) if self.latencies else None,
            "latency_ms_max": round(max(self.latencies), 1) if self.latencies else None,
            "event_rows": self.store.count(),
            "counts_by_type": self.store.counts_by_type(),
        }
        self.store.close()
        return s


# --- overlay rendering (shared by the cv2 window and the web GUI) --------------------------------

_GREEN = (80, 220, 80)
_RED = (60, 60, 235)
_YELLOW = (40, 200, 235)
_WHITE = (240, 240, 240)


def _label(img, text: str, org, color) -> None:
    import cv2

    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    x, y = int(org[0]), int(org[1])
    cv2.rectangle(img, (x - 2, y - th - 4), (x + tw + 2, y + 2), (20, 20, 20), -1)
    cv2.putText(img, text, (x, y - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)


def _risk_color(risk: float):
    return _GREEN if risk < 0.5 else (_YELLOW if risk < 0.85 else _RED)


def annotate(result, active, gaze, kp_conf: float, stats: dict, ppe_boxes=None, fall_risk=None,
             equipment_boxes=None, inspection_boxes=None):
    """Draw skeleton, boxes, torso tilt, fall-risk meter, PPE, equipment, gaze tags, HUD, banner."""

    import cv2

    img = result.frame.image.copy()
    h, w = img.shape[:2]
    fall_ids = {hz.track_id for hz in active if hz.hazard_type is HazardType.FALL}
    has_active = len(active) > 0
    fall_risk = fall_risk or {}

    for p in result.persons:
        is_fall = p.track_id in fall_ids
        risk = float(fall_risk.get(p.track_id, 0.0))
        color = _RED if is_fall else _GREEN
        kps = p.keypoints
        for a, b in _SKELETON:
            if kps[a, 2] >= kp_conf and kps[b, 2] >= kp_conf:
                cv2.line(img, (int(kps[a, 0]), int(kps[a, 1])),
                         (int(kps[b, 0]), int(kps[b, 1])), color, 2, cv2.LINE_AA)
        for i in range(len(kps)):
            if kps[i, 2] >= kp_conf:
                cv2.circle(img, (int(kps[i, 0]), int(kps[i, 1])), 3, color, -1, cv2.LINE_AA)

        x1, y1, x2, y2 = (int(v) for v in p.bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)

        tm = torso_metrics(p, h, kp_conf)
        label = f"id{p.track_id}"
        if tm is not None:
            label += f"  tilt {tm[1]:.0f}"
        _label(img, label, (x1, max(14, y1 - 6)), color)

        # fall-risk meter just below the box
        by = min(h - 8, y2 + 5)
        bw = max(40, x2 - x1)
        cv2.rectangle(img, (x1, by), (x1 + bw, by + 6), (55, 55, 55), -1)
        cv2.rectangle(img, (x1, by), (x1 + int(bw * max(0.0, min(1.0, risk))), by + 6),
                      _risk_color(risk), -1)
        _label(img, f"fall risk {risk:.2f}", (x1, by + 22), _risk_color(risk))

        ty = by + 40
        if is_fall:
            _label(img, "FALL", (x1, ty), _RED)
            ty += 20
        if has_active:
            looking = any(gaze.looking_at(p, hz.bbox) for hz in active if hz.track_id != p.track_id)
            _label(img, "WATCHING" if looking else "NOT LOOKING", (x1, ty),
                   _GREEN if looking else _YELLOW)

    # PPE detections (compliant = teal, violation = red)
    ppe_viol = 0
    for box in (ppe_boxes or []):
        bx1, by1, bx2, by2 = (int(v) for v in box["bbox"])
        c = (150, 230, 120) if box["ok"] else _RED
        if not box["ok"]:
            ppe_viol += 1
        cv2.rectangle(img, (bx1, by1), (bx2, by2), c, 2)
        _label(img, box["label"], (bx1, max(12, by1 - 4)), c)

    # equipment / vehicles (orange)
    for eq in (equipment_boxes or []):
        ex1, ey1, ex2, ey2 = (int(v) for v in eq["bbox"])
        cv2.rectangle(img, (ex1, ey1), (ex2, ey2), (0, 140, 255), 2)
        _label(img, eq["kind"], (ex1, max(12, ey1 - 4)), (0, 140, 255))

    # structural defects — inspection mode (purple, severity-coded label)
    for box in (inspection_boxes or []):
        bx1, by1, bx2, by2 = (int(v) for v in box["bbox"])
        sev = box.get("severity_score", 0.0)
        c = (200, 60, 220) if sev <= 0.85 else (60, 60, 235)
        cv2.rectangle(img, (bx1, by1), (bx2, by2), c, 2)
        _label(img, box["label"], (bx1, max(12, by1 - 4)), c)

    # HUD panel (top-left)
    lines = [
        f"FieldPilot AI   [{stats.get('perspective', '')}]",
        f"FPS {stats.get('fps', 0)}    infer {stats.get('infer_ms', '-')} ms",
        f"persons {stats.get('persons', 0)}   tracks {stats.get('unique_tracks', 0)}",
        f"hazards {stats.get('hazards', 0)}   alerts {stats.get('alerts', 0)}",
        f"PPE violations (frame): {ppe_viol}",
    ]
    pw, ph = 260, 20 * len(lines) + 12
    overlay = img.copy()
    cv2.rectangle(overlay, (8, 8), (8 + pw, 8 + ph), (18, 18, 18), -1)
    cv2.addWeighted(overlay, 0.55, img, 0.45, 0, img)
    for i, line in enumerate(lines):
        cv2.putText(img, line, (16, 30 + i * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, _WHITE, 1, cv2.LINE_AA)

    # hazard banner (top, full width)
    if has_active:
        types = ",".join(sorted({hz.hazard_type.value for hz in active}))
        cv2.rectangle(img, (0, 0), (w, 6), _RED, -1)
        _label(img, f"! HAZARD ACTIVE: {types}", (w - 340, 26), _RED)
    return img


def encode_jpeg(img, quality: int = 75) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""
