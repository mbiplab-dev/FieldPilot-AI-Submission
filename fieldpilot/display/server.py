"""Web GUI: live annotated feed + analysis dashboard.

Runs the safety pipeline inside the server's event loop, writing each annotated frame and a stats
snapshot into a shared LiveState. The browser shows the feed as an MJPEG stream and polls `/stats`
for the analysis panel and hazard event feed.

    uv run python -m fieldpilot.display.server         # or: python -m fieldpilot.run --gui
    open http://localhost:8000

Two ingest paths exist, and they are deliberately the *same* pipeline code:

- server camera — `/dev/videoN` (or a file / synthetic source) read by `VideoSource`, annotated
  server-side and served as MJPEG on `/stream`.
- browser camera — the site manager's own browser captures the camera with `getUserMedia` and ships
  JPEG frames to `/ws/video`; we answer with JSON detections that the browser draws on a canvas.
  This is what lets FieldPilot run on a laptop or a phone, where the camera belongs to the browser
  and the server has no camera at all. Hazards raised from browser frames go through the very same
  event bridge, so bus → triggers → rules → alerts behaves identically for both paths.
- worker phone — the worker app also connects to `/ws/video`, identified by `worker_id`, but ships
  raw NV21 camera planes instead of JPEG. Encoding JPEG on the handset was the bottleneck keeping
  it under 2 fps; `decode_frame` tells the two payload kinds apart by magic bytes and converts
  either to the same BGR ndarray the pipeline already expects.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from fieldpilot.core.config import Config, load_config
from fieldpilot.core.pipeline import Pipeline
from fieldpilot.core.types import Frame
from fieldpilot.core.video_source import VideoSource
from fieldpilot.display.feeds import FeedRegistry
from fieldpilot.display.state import LiveState
from fieldpilot.logging_.logger import get_logger, jsonl_append, setup_logging

log = get_logger("fieldpilot.gui")


class _ForwardingBridge:
    """Wraps the bus bridge so every event is also queued for the central API.

    The bus carries events to a backend on the same network; the outbox is what survives that
    network going away. Enqueue happens even if the bus publish fails, because the durable copy
    is the one that guarantees the event is not lost.
    """

    def __init__(self, inner, forwarder) -> None:
        self._inner = inner
        self._forwarder = forwarder

    async def emit(self, hazard):
        try:
            event = await self._inner.emit(hazard)
        except Exception:  # noqa: BLE001 — bus down must not stop the durable path
            log.exception("bus publish failed — event still queued for store-and-forward")
            from fieldpilot.events.bridge import hazard_to_event

            event = hazard_to_event(
                hazard, camera_id=self._inner.camera_id, zone=self._inner.zone
            )
        await self._forwarder.submit(event.model_dump_json_safe())
        return event


def _apply_settings(pipeline, settings: dict) -> None:
    """Apply an operator settings change from `control.settings` to the running detectors.

    The backend owns persistence; the edge owns the live models. A partial payload is normal —
    the dashboard publishes only the key that changed — so every field is optional here.

    Failures are logged and swallowed: a bad settings payload must never stop the safety loop.
    """

    ppe = getattr(pipeline, "ppe", None)

    if "tracked_items" in settings and ppe is not None:
        try:
            applied = ppe.set_tracked_items(settings["tracked_items"])
            log.info("control.settings -> tracked PPE items now %s",
                     ", ".join(sorted(applied)) or "(none)")
        except Exception:  # noqa: BLE001
            log.exception("could not apply tracked_items %r", settings["tracked_items"])

    if "confidence_threshold" in settings:
        try:
            value = float(settings["confidence_threshold"])
        except (TypeError, ValueError):
            log.warning("ignoring non-numeric confidence_threshold %r",
                        settings["confidence_threshold"])
        else:
            for target in (ppe, getattr(pipeline, "engine", None)):
                if target is not None and hasattr(target, "conf_min"):
                    target.conf_min = value
            log.info("control.settings -> confidence threshold %.2f", value)

    if "selected_model" in settings:
        # Swapping detector weights means reloading a model on the inference thread, which the
        # pipeline does not currently support mid-run. Say so plainly instead of silently
        # accepting a change that did not take effect.
        log.warning(
            "control.settings -> selected_model=%r recorded by the backend, but the edge cannot "
            "hot-swap detector weights; restart the edge to pick it up",
            settings["selected_model"],
        )


def _build_source(cfg: Config, kind: str | None, file_path: str | None) -> VideoSource:
    v = cfg.section("video")
    return VideoSource(
        kind=kind or v.get("source", "webcam"),
        webcam_index=int(v.get("webcam_index", 0)),
        file_path=file_path or v.get("file_path"),
        target_fps=int(v.get("target_fps", 30)),
        queue_maxsize=int(v.get("queue_maxsize", 4)),
        pace=True,
    )


# --- browser-webcam ingest -----------------------------------------------------------------------

_CLASS_RE = re.compile(r"[^a-z0-9]+")
_DEFECT_SEVERE = 0.85


def normalise_class(name: str) -> str:
    """`NO-Hardhat` / `Safety Vest` → `no_hardhat` / `safety_vest`.

    Detector class names vary per checkpoint; the wire format must not. Normalising here means the
    browser overlay and any consumer can switch on a stable identifier.
    """

    return _CLASS_RE.sub("_", str(name).strip().lower()).strip("_")


def decode_jpeg(payload: bytes) -> np.ndarray | None:
    """JPEG bytes → BGR ndarray, or None when the payload is not a decodable image."""

    if not payload:
        return None
    buf = np.frombuffer(payload, dtype=np.uint8)
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


#: `camera_stream.dart` prefixes every raw frame with this so the socket can tell a phone's raw
#: planes apart from the browser page's JPEG bytes without relying on payload length.
_RAW_MAGIC = b"FPR1"
_RAW_HEADER_LEN = 12  # magic(4) + width(2) + height(2) + stride(2) + format(2), all big-endian
_RAW_FORMAT_NV21 = 1


def _decode_raw_nv21(payload: bytes) -> np.ndarray | None:
    """The phone's raw-plane payload → BGR ndarray, or None when it is not a usable NV21 frame.

    `startImageStream` hands the phone a single NV21 plane whose row stride can exceed its pixel
    width (the camera pads rows for hardware alignment). Reshaping straight to `(rows, width)`
    would read that padding as the start of the next row and shear the picture, so the buffer is
    reshaped to the *reported* stride first and only then sliced down to `width`.
    """

    if len(payload) < _RAW_HEADER_LEN:
        return None
    width, height, stride, fmt = struct.unpack_from(">HHHH", payload, 4)
    if fmt != _RAW_FORMAT_NV21 or width <= 0 or height <= 0 or stride < width:
        return None

    plane_rows = height * 3 // 2  # NV21: `height` Y rows + `height // 2` interleaved-VU rows
    expected = stride * plane_rows
    plane = payload[_RAW_HEADER_LEN:]
    if len(plane) < expected:
        return None

    try:
        yuv = np.frombuffer(plane, dtype=np.uint8, count=expected).reshape(plane_rows, stride)
        if stride != width:
            yuv = np.ascontiguousarray(yuv[:, :width])
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV21)
    except (ValueError, cv2.error):
        # a header that lies about its own geometry must not take the socket down with it
        return None


def decode_frame(payload: bytes) -> np.ndarray | None:
    """Either wire kind on `/ws/video` → BGR ndarray, or None when the payload is undecodable.

    The browser-camera page still ships plain JPEG; a signed-in phone ships raw NV21 planes
    (encoding JPEG on the handset was the frame-rate bottleneck this replaces). The two are told
    apart by magic, not length, so neither producer needs to know the other exists.
    """

    if payload[:4] == _RAW_MAGIC:
        return _decode_raw_nv21(payload)
    return decode_jpeg(payload)


def _detect(pipeline: Pipeline, frame: Frame):
    """Run the pipeline's own per-frame work: inference, then every safety detector in order.

    `Pipeline._process` is the single place that runs fall → PPE → inspection → proximity →
    attention and maintains the active-hazard registry. Driving *it* — rather than re-listing the
    detectors here — is what guarantees a browser frame is treated exactly like a `/dev/video0`
    frame, including tracker state, cooldowns and the attention state machine. `pipeline.py` is
    owned elsewhere, so no new public entry point was added for this; both calls run in one
    executor submission so the event loop is never blocked by GPU work.
    """

    result = pipeline.engine.infer(frame)
    return result, pipeline._process(result)


def _boxes_for(result, pipeline: Pipeline, hazards: list) -> tuple[list[dict], int]:
    """Flatten persons + PPE + equipment + structural defects into one wire-format box list."""

    flagged = {h.track_id for h in hazards if h.track_id is not None}
    out: list[dict] = []
    for p in result.persons:
        out.append({
            "class": "person",
            "category": "person",
            "confidence": round(float(p.conf), 3),
            "box": [round(float(v), 1) for v in p.bbox],
            "track_id": p.track_id,
            "is_violation": p.track_id in flagged,
        })

    violations = 0
    for box in pipeline.ppe.last_boxes:
        ok = bool(box.get("ok", True))
        violations += 0 if ok else 1
        conf = box.get("conf")
        out.append({
            "class": normalise_class(box.get("label", "")),
            "category": "ppe",
            "ppe_item": box.get("cat"),
            "confidence": round(float(conf), 3) if conf is not None else None,
            "box": [round(float(v), 1) for v in box["bbox"]],
            "is_violation": not ok,
        })

    for eq in pipeline.ppe.equipment_boxes:
        out.append({
            "class": normalise_class(eq.get("label", eq.get("kind", ""))),
            "category": "equipment",
            "kind": eq.get("kind"),
            "confidence": None,
            "box": [round(float(v), 1) for v in eq["bbox"]],
            "is_violation": False,
        })

    for box in pipeline.inspection.last_boxes:
        severity = float(box.get("severity_score", 0.0))
        out.append({
            "class": normalise_class(box.get("label", "")),
            "category": "defect",
            "severity_score": round(severity, 2),
            "confidence": None,
            "box": [round(float(v), 1) for v in box["bbox"]],
            "is_violation": severity >= _DEFECT_SEVERE,
        })
    return out, violations


def _poses_for(result, kp_conf: float) -> list[dict]:
    """COCO-17 keypoints per tracked person, as [x, y, confidence] triples."""

    poses = []
    for p in result.persons:
        kps = [
            [round(float(x), 1), round(float(y), 1), round(float(c), 3)]
            for x, y, c in p.keypoints
        ]
        poses.append({
            "track_id": p.track_id,
            "keypoints": kps,
            "visible": sum(1 for k in kps if k[2] >= kp_conf),
        })
    return poses


def _hazard_json(event) -> dict:
    return {
        "id": event.id,
        "type": event.category(),
        "severity": event.severity.value,
        "message": event.message,
        "track_id": event.track_id,
        "bbox": [round(float(v), 1) for v in event.bbox] if event.bbox is not None else None,
        "ts_wall": event.ts_wall,
    }


async def _publish_hazard(pipeline: Pipeline, event, result, sink) -> None:
    """Do with a browser-sourced hazard exactly what `Pipeline.run` does with a camera-sourced one.

    Same durable log, same alert snapshot, same event bridge (bus → triggers → rules → alerts) when
    one is configured, and the same local earcon/TTS dispatch when it is not. Browser frames must
    not become a second, parallel universe.
    """

    pipeline.store.record_event(event, alerted=True)
    record = None
    if pipeline.event_bridge is not None:
        pipeline._save_alert_image(event, result.frame.image)
        await pipeline.event_bridge.emit(event)
    else:
        record = pipeline.dispatcher.dispatch(event)
    jsonl_append(pipeline.jsonl_path, {
        "event": event,
        "admitted": record.admitted if record else None,
        "latency_ms": round(record.latency_ms, 1) if record else None,
        "routed": "bus" if pipeline.event_bridge is not None else "dispatcher",
        "ingest": "browser",
    })
    if sink is not None:
        sink.add_event({
            "type": event.category(),
            "severity": event.severity.value,
            "message": event.message,
            "track_id": event.track_id,
            "latency_ms": (round(record.latency_ms, 1)
                           if record is not None and record.admitted else None),
            "ts_wall": event.ts_wall,
            "source": "browser",
        })


async def process_browser_frame(pipeline: Pipeline, image: np.ndarray, index: int,
                                *, sink=None, origin: FrameOrigin | None = None) -> dict:
    """One browser frame → one JSON reply, with every hazard routed like a camera hazard."""

    loop = asyncio.get_running_loop()
    height, width = int(image.shape[0]), int(image.shape[1])
    # phones rotate and browsers renegotiate resolution mid-stream, so re-declare it every frame
    # (the estimator only resets its history when the size actually changes).
    pipeline.gaze.set_frame_size(width, height)
    frame = Frame(index=index, ts_monotonic=time.monotonic(), image=image)

    started = time.perf_counter()
    result, hazards = await loop.run_in_executor(None, _detect, pipeline, frame)
    inference_ms = round((time.perf_counter() - started) * 1000.0, 1)

    pipeline.frame_count += 1
    for event in hazards:
        pipeline.hazard_count += 1
        pipeline._type_counts[event.category()] = pipeline._type_counts.get(event.category(), 0) + 1
        # so a reviewer can tell which camera path produced the alert
        event.meta.setdefault("ingest", origin.ingest if origin else "browser")
        if origin is not None:
            # Whose phone saw this. NOT the same as the hazard's own worker_id, which is the
            # *tracked person in frame* — a rear camera mostly sees colleagues, not its owner.
            event.meta.setdefault("source_worker", origin.worker_id)
            event.meta.setdefault("source_camera_id", origin.camera_id)
            if origin.zone:
                event.meta.setdefault("source_zone", origin.zone)
        await _publish_hazard(pipeline, event, result, sink)

    detections, violations = _boxes_for(result, pipeline, hazards)
    poses = _poses_for(result, pipeline.kp_conf)
    return {
        "frame": {"index": index, "width": width, "height": height},
        "detections": detections,
        "poses": poses,
        "counts": {
            "people": len(result.persons),
            "ppe_items": len(pipeline.ppe.last_boxes),
            "violations": violations,
            "poses": sum(1 for p in poses if p["visible"] > 0),
        },
        "inference_ms": inference_ms,
        "hazards": [_hazard_json(e) for e in hazards],
        "active_hazards": [
            {"type": h.hazard_type.value, "track_id": h.track_id} for h in pipeline._active
        ],
    }


@dataclass(frozen=True)
class FrameOrigin:
    """Who is sending these frames, for a socket that carries an identified device.

    The browser-camera page sends none of this and stays anonymous; a signed-in phone sends all of
    it, which is what lets the manager see "Ravi's feed" rather than "some camera".
    """

    worker_id: str
    zone: str | None = None
    display_name: str | None = None
    ingest: str = "phone"

    @property
    def camera_id(self) -> str:
        """Stable per-device id, so alerts record which phone saw the hazard."""

        return f"phone-{self.worker_id}"


#: Box colours (BGR) for the relayed feed, matching the dashboard's severity language.
_BOX_OK = (120, 190, 120)
_BOX_VIOLATION = (60, 60, 220)


def annotate(image: np.ndarray, detections: list[dict]) -> np.ndarray:
    """Draw detections onto a copy of the frame for the manager's relayed view.

    The phone draws its own overlay from the JSON reply, exactly as the browser page does. The
    manager's feed is a plain MJPEG stream with no client-side canvas to draw on, so the boxes have
    to be burned in here. Drawing on a copy keeps the pristine frame for alert snapshots.
    """

    canvas = image.copy()
    for det in detections:
        box = det.get("box") or []
        if len(box) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(round(float(v))) for v in box)
        except (TypeError, ValueError):
            # One unusable box must not cost the manager the whole annotated frame.
            continue
        violation = bool(det.get("is_violation"))
        colour = _BOX_VIOLATION if violation else _BOX_OK
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, 2 if violation else 1)
        label = str(det.get("class") or det.get("category") or "")
        if label:
            cv2.putText(canvas, label, (x1, max(12, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)
    return canvas


def encode_jpeg(image: np.ndarray, quality: int = 70) -> bytes | None:
    """JPEG-encode a frame for relay. Quality is modest on purpose — this is a monitoring view."""

    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else None


def render_relay_frame(image: np.ndarray, detections: list[dict]) -> bytes | None:
    """Annotate and encode in one call, so the whole CPU cost moves to a worker thread together."""

    return encode_jpeg(annotate(image, detections))


class LatestFrame:
    """Single-slot mailbox with drop-oldest semantics.

    A browser can encode frames faster than the server can infer on them. Queueing would grow
    unbounded latency and memory for no benefit — a stale frame is worthless for safety — so a new
    frame simply replaces the one still waiting, mirroring `VideoSource`'s bounded drop-oldest
    queue. `dropped` is reported back to the client so the overlay can show the real ingest rate.
    """

    def __init__(self) -> None:
        self._payload: bytes | None = None
        self._ready = asyncio.Event()
        self._closed = False
        self.dropped = 0

    def put(self, payload: bytes) -> None:
        if self._payload is not None:
            self.dropped += 1
        self._payload = payload
        self._ready.set()

    def close(self) -> None:
        self._closed = True
        self._ready.set()

    async def get(self) -> bytes | None:
        """Newest pending frame, or None once the producer has closed and drained."""

        while self._payload is None and not self._closed:
            self._ready.clear()
            await self._ready.wait()
        payload, self._payload = self._payload, None
        return payload


async def _read_frames(websocket: WebSocket, slot: LatestFrame) -> None:
    """Drain the socket as fast as it delivers, keeping only the newest frame."""

    try:
        while True:
            slot.put(await websocket.receive_bytes())
    except WebSocketDisconnect:
        pass
    except (KeyError, RuntimeError, ValueError):
        # a text frame (no "bytes" key) or a socket already closed — treat as end of stream
        log.debug("browser camera reader stopped", exc_info=True)
    finally:
        slot.close()


def create_app(cfg: Config, source_kind: str | None = None, file_path: str | None = None,
               with_bus: bool = False) -> FastAPI:
    state = LiveState()

    forward_holder: dict = {}
    # browser-camera ingest: its own Pipeline (built on first WebSocket frame) sharing this app's
    # event bridge. A *separate* instance because BoT-SORT track state, fall-velocity buffers and
    # PPE cooldowns are per-camera — feeding two cameras through one set of detectors would cross
    # their track IDs. The bridge, and therefore the whole downstream platform, is shared.
    browser_holder: dict = {}
    browser_build_lock = asyncio.Lock()
    browser_gate = asyncio.Lock()
    # Latest annotated frame per streaming phone, for the manager's live view.
    feeds = FeedRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        source = _build_source(cfg, source_kind, file_path)
        bus = None
        bridge = None
        docs = None
        forwarder = None
        pipeline_holder: dict = {}

        # Offline store-and-forward: when a central API is configured, every event the edge
        # produces is written to a local SQLite outbox and forwarded with retry. Site Wi-Fi
        # dropping must not lose hazards, so this is durable-first, network-second.
        central_api = cfg.get("storage.central_api")
        if central_api:
            from fieldpilot.offline import OUTBOX_TABLE, Outbox, StoreAndForward
            from fieldpilot.storage import DocStore

            docs = DocStore("sqlite", str(cfg.get("storage.sqlite_path", "data/fieldpilot.db")))
            await docs.start([OUTBOX_TABLE])
            forwarder = StoreAndForward(Outbox(docs), central_api=str(central_api))
            await forwarder.start()
            forward_holder["forwarder"] = forwarder
            log.info("store-and-forward enabled → %s", central_api)

        if with_bus:
            from fieldpilot.events.bridge import PipelineEventBridge
            from fieldpilot.events.bus import create_bus

            bus = create_bus(cfg.get("events.bus_backend", "memory"),
                             cfg.get("events.redis_url", "redis://localhost:6379/0"))
            await bus.start()
            bridge = PipelineEventBridge(bus, camera_id=cfg.get("events.camera_id", "cam-edge-0"),
                                         zone=cfg.get("events.zone"))
            if forwarder is not None:
                bridge = _ForwardingBridge(bridge, forwarder)

            async def _on_control(topic: str, msg: dict) -> None:
                p = pipeline_holder.get("pipeline")
                if p is None:
                    return
                if topic == "control.inspection":
                    actual = p.set_inspection(bool(msg.get("enabled")))
                    log.info("control.inspection -> inspection mode %s",
                             "ON" if actual else "OFF")
                elif topic == "control.settings":
                    _apply_settings(p, msg)

            await bus.subscribe("control.inspection", _on_control)
            # operator settings changed on the dashboard reach the running detectors here, so a
            # PPE item switched off stops raising violations without an edge restart
            await bus.subscribe("control.settings", _on_control)
            log.info("GUI in bus mode — detections publish to %s",
                     cfg.get("events.bus_backend", "memory"))

        pipeline = Pipeline(cfg, sink=state, event_bridge=bridge)
        pipeline_holder["pipeline"] = pipeline
        browser_holder["bridge"] = bridge
        task = asyncio.create_task(pipeline.run(source))
        log.info("GUI pipeline started — open http://localhost:8000")
        try:
            yield
        finally:
            state.running = False
            source.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            browser = browser_holder.pop("pipeline", None)
            if browser is not None:
                browser.dispatcher.shutdown()
                browser.store.close()
            if forwarder is not None:
                await forwarder.stop()
            if docs is not None:
                await docs.stop()
            if bus is not None:
                await bus.stop()

    app = FastAPI(title="FieldPilot AI", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _PAGE

    @app.get("/stats")
    async def stats() -> JSONResponse:
        return JSONResponse({**state.snapshot(), "worker_feeds": feeds.stats()})

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen():
            boundary = b"--frame\r\n"
            while state.running:
                jpeg = state.get_jpeg()
                if jpeg:
                    yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(1 / 25)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    async def browser_pipeline() -> Pipeline:
        """The browser-ingest pipeline, built once, lazily (loading weights costs seconds + VRAM)."""

        async with browser_build_lock:
            pipeline = browser_holder.get("pipeline")
            if pipeline is None:
                pipeline = await asyncio.to_thread(
                    Pipeline, cfg, sink=None, event_bridge=browser_holder.get("bridge"),
                )
                browser_holder["pipeline"] = pipeline
                log.info("browser-camera pipeline ready (bridge=%s)",
                         "bus" if browser_holder.get("bridge") is not None else "local dispatcher")
            return pipeline

    @app.get("/camera", response_class=HTMLResponse)
    async def camera_page() -> str:
        """Zero-build-step capture page, so browser ingest works without the Next.js app."""

        return _CAMERA_PAGE

    @app.websocket("/ws/video")
    async def ws_video(
        websocket: WebSocket,
        worker_id: str | None = None,
        zone: str | None = None,
        name: str | None = None,
    ) -> None:
        """Captured JPEG frames in, JSON detections out.

        Two producers share this socket. The browser-camera page connects anonymously. A signed-in
        phone passes `worker_id` (and its current `zone`), which does two extra things: hazards
        record which device saw them, and the annotated frame is relayed to the manager under that
        worker's name. All the AI runs here on the server either way — the phone only ships pixels.
        """

        await websocket.accept()
        try:
            pipeline = await browser_pipeline()
        except Exception as exc:  # noqa: BLE001 — no detector means say so, not die silently
            log.exception("browser-camera pipeline could not be built")
            with suppress(Exception):
                await websocket.send_json({"error": f"detector unavailable: {exc}"})
            with suppress(Exception):
                await websocket.close(code=1011)
            return

        origin = (
            FrameOrigin(worker_id=worker_id.strip(), zone=(zone or "").strip() or None,
                        display_name=(name or "").strip() or None)
            if worker_id and worker_id.strip()
            else None
        )
        if origin is not None:
            feeds.open(origin.worker_id, zone=origin.zone, display_name=origin.display_name)
            log.info("phone camera stream opened for %s (zone=%s)", origin.worker_id, origin.zone)

        slot = LatestFrame()
        reader = asyncio.create_task(_read_frames(websocket, slot))
        index = 0
        try:
            while True:
                payload = await slot.get()
                if payload is None:
                    break
                image = await asyncio.to_thread(decode_frame, payload)
                if image is None or image.size == 0:
                    log.warning("ignored an undecodable camera frame (%d bytes)", len(payload))
                    await websocket.send_json({
                        "error": "The camera frame could not be decoded as JPEG or a raw NV21 plane.",
                        "dropped": slot.dropped,
                    })
                    continue
                try:
                    async with browser_gate:
                        body = await process_browser_frame(
                            pipeline, image, index, sink=state, origin=origin,
                        )
                except (WebSocketDisconnect, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad frame must not drop the socket
                    log.exception("camera frame processing failed")
                    await websocket.send_json({"error": f"Frame processing failed: {exc}"})
                    continue
                index += 1
                body["dropped"] = slot.dropped

                if origin is not None:
                    # Relay to the manager. Encoding is worth a thread — it is pure CPU and would
                    # otherwise stall the event loop for every other connected phone.
                    try:
                        relay = await asyncio.to_thread(
                            render_relay_frame, image, body["detections"])
                        pristine = await asyncio.to_thread(encode_jpeg, image, 92)
                        if relay and pristine:
                            feeds.publish(origin.worker_id, relay,
                                          width=body["frame"]["width"],
                                          height=body["frame"]["height"],
                                          hazards=len(body["hazards"]),
                                          raw_jpeg=pristine,
                                          detections=body["detections"])
                    except Exception:  # noqa: BLE001 — a failed relay must not stop detection
                        log.exception("could not relay a frame for %s", origin.worker_id)

                await websocket.send_text(json.dumps(body))
        except WebSocketDisconnect:
            log.info("camera socket disconnected after %d frame(s)", index)
        except RuntimeError:
            # send on an already-closed socket — the client is gone, nothing to report to
            log.debug("camera socket closed mid-send", exc_info=True)
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader
            if origin is not None:
                feeds.close(origin.worker_id)
                log.info("phone camera stream closed for %s", origin.worker_id)

    # ------------------------------------------------------------ worker camera feeds

    @app.get("/workers/live")
    async def workers_live() -> JSONResponse:
        """Which workers' phones are streaming right now."""

        return JSONResponse({"feeds": feeds.list(), "stats": feeds.stats()})

    @app.get("/workers/{worker_id}/stream")
    async def worker_stream(worker_id: str) -> StreamingResponse:
        """One worker's phone camera as MJPEG, annotated with what the server detected.

        Served at the viewer's pace with latest-frame-wins semantics: a slow dashboard skips
        frames rather than falling progressively further behind a live safety feed.
        """

        async def gen():
            last_sent: bytes | None = None
            idle = 0.0
            boundary = b"--frame\r\n"
            while True:
                jpeg = feeds.frame(worker_id)
                if jpeg is not None and jpeg is not last_sent:
                    last_sent = jpeg
                    idle = 0.0
                    yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                else:
                    idle += 1 / 15
                    # The phone stopped sending (backgrounded, out of signal, battery saver).
                    # End the response rather than hold a socket open on a dead feed.
                    if idle > 30.0:
                        return
                await asyncio.sleep(1 / 15)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/workers/{worker_id}/capture")
    async def worker_capture(worker_id: str) -> JSONResponse:
        """Newest pristine phone frame and its model boxes, captured only when requested."""

        snapshot = feeds.capture(worker_id)
        if snapshot is None:
            return JSONResponse({"detail": "worker has no capturable frame"}, status_code=404)
        jpeg = snapshot.pop("jpeg")
        snapshot["jpeg_base64"] = base64.b64encode(jpeg).decode("ascii")
        return JSONResponse(snapshot)

    @app.get("/offline/status")
    async def offline_status() -> JSONResponse:
        """Queue depth and connectivity for the store-and-forward path."""

        forwarder = forward_holder.get("forwarder")
        if forwarder is None:
            return JSONResponse({
                "enabled": False,
                "reason": "storage.central_api is not configured — edge runs offline-only",
            })
        return JSONResponse({"enabled": True, **await forwarder.status()})

    @app.post("/offline/flush")
    async def offline_flush() -> JSONResponse:
        """Force a drain attempt (the flusher also runs on its own schedule)."""

        forwarder = forward_holder.get("forwarder")
        if forwarder is None:
            return JSONResponse({"enabled": False, "sent": 0, "failed": 0})
        return JSONResponse({"enabled": True, **await forwarder.flush_once()})

    return app


def run_gui(config_path: str = "config.yaml", source_kind: str | None = None,
            file_path: str | None = None, host: str = "0.0.0.0", port: int = 8000,
            with_bus: bool = False) -> int:
    import uvicorn

    cfg = load_config(config_path)
    setup_logging(cfg.get("logging.level", "INFO"))
    app = create_app(cfg, source_kind, file_path, with_bus=with_bus)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FieldPilot AI — Live</title>
<style>
  :root{--bg:#0d1017;--panel:#161b26;--line:#232b3a;--txt:#e6e9ef;--dim:#8b94a7;
        --hi:#ff5252;--med:#ffab40;--low:#ffd54f;--ok:#4ade80;--accent:#5b9dff}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.4 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
  header{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{font-size:16px;margin:0;letter-spacing:.3px;font-weight:600}
  header .tag{font-size:11px;color:var(--dim);border:1px solid var(--line);padding:2px 8px;border-radius:99px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .wrap{display:grid;grid-template-columns:1fr 340px;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}}
  .feed{background:#000;border:1px solid var(--line);border-radius:12px;overflow:hidden;position:relative}
  .feed img{display:block;width:100%;height:auto}
  .banner{position:absolute;top:0;left:0;right:0;padding:8px 12px;background:rgba(255,82,82,.92);
          color:#fff;font-weight:600;font-size:13px;display:none}
  .side{display:flex;flex-direction:column;gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim)}
  .tiles{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .tile{background:#0f131c;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
  .tile .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
  .tile .k{font-size:11px;color:var(--dim);margin-top:2px}
  .feedlist{display:flex;flex-direction:column;gap:8px;max-height:340px;overflow:auto}
  .ev{border-left:3px solid var(--dim);padding:7px 10px;background:#0f131c;border-radius:6px}
  .ev.high{border-color:var(--hi)} .ev.medium{border-color:var(--med)} .ev.low{border-color:var(--low)}
  .ev .t{font-size:11px;color:var(--dim);display:flex;justify-content:space-between}
  .ev .m{margin-top:2px}
  .empty{color:var(--dim);font-size:13px;padding:6px 0}
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>FieldPilot AI</h1><span class="tag">Live Safety Monitor</span>
  <span class="tag" id="persp">—</span>
  <span style="flex:1"></span>
  <span class="tag" id="uptime">up 0s</span>
</header>
<div class="wrap">
  <div class="feed">
    <div class="banner" id="banner"></div>
    <img src="/stream" alt="live feed"/>
  </div>
  <div class="side">
    <div class="card">
      <h2>Live analysis</h2>
      <div class="tiles">
        <div class="tile"><div class="v" id="fps">–</div><div class="k">FPS</div></div>
        <div class="tile"><div class="v" id="infer">–</div><div class="k">inference (ms)</div></div>
        <div class="tile"><div class="v" id="persons">–</div><div class="k">persons in view</div></div>
        <div class="tile"><div class="v" id="tracks">–</div><div class="k">unique tracks</div></div>
        <div class="tile"><div class="v" id="hazards">–</div><div class="k">hazards</div></div>
        <div class="tile"><div class="v" id="alerts">–</div><div class="k">alerts fired</div></div>
      </div>
    </div>
    <div class="card">
      <h2>Hazard event feed</h2>
      <div class="feedlist" id="events"><div class="empty">No events yet.</div></div>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
function fmt(ts){try{return new Date(ts*1000).toLocaleTimeString()}catch(e){return ''}}
async function tick(){
  try{
    const r=await fetch('/stats'); const d=await r.json(); const s=d.stats||{};
    $('fps').textContent=s.fps??'–'; $('infer').textContent=s.infer_ms??'–';
    $('persons').textContent=s.persons??'–'; $('tracks').textContent=s.unique_tracks??'–';
    $('hazards').textContent=s.hazards??'–'; $('alerts').textContent=s.alerts??'–';
    $('persp').textContent=s.perspective||'—'; $('uptime').textContent='up '+(d.uptime_s??0)+'s';
    $('dot').style.background=d.running?'var(--ok)':'var(--dim)';
    const act=s.active_hazards||[]; const b=$('banner');
    if(act.length){b.style.display='block';b.textContent='⚠ HAZARD ACTIVE — '+act.map(a=>a.type+' (id'+a.track_id+')').join(', ');}
    else b.style.display='none';
    const ev=d.events||[]; const box=$('events');
    if(!ev.length){box.innerHTML='<div class="empty">No events yet.</div>';}
    else box.innerHTML=ev.map(e=>`<div class="ev ${e.severity}"><div class="t"><span>${e.type}${e.track_id!=null?' · id'+e.track_id:''}</span><span>${fmt(e.ts_wall)}${e.latency_ms!=null?' · '+e.latency_ms+'ms':''}</span></div><div class="m">${e.message||''}</div></div>`).join('');
  }catch(e){$('dot').style.background='var(--hi)';}
}
setInterval(tick,500); tick();
</script>
</body></html>"""


_CAMERA_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FieldPilot AI — Browser camera</title>
<style>
  :root{--bg:#0d1017;--panel:#161b26;--line:#232b3a;--txt:#e6e9ef;--dim:#8b94a7;--hi:#ff5252;--ok:#4ade80}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
  header{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
  h1{font-size:16px;margin:0;font-weight:600}
  select,button{font:inherit;background:#0f131c;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
  button{cursor:pointer}
  .wrap{padding:16px;max-width:1000px;margin:0 auto}
  .stage{position:relative;background:#000;border:1px solid var(--line);border-radius:12px;overflow:hidden}
  video,canvas{display:block;width:100%;height:auto}
  canvas{position:absolute;inset:0}
  .row{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;color:var(--dim);font-variant-numeric:tabular-nums}
  .row b{color:var(--txt)}
  .msg{margin:12px 0 0;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
  .msg.bad{border-color:var(--hi);color:#ffc9c9}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--dim)}
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span><h1>FieldPilot AI</h1>
  <span style="color:var(--dim)">browser camera → edge inference</span>
  <span style="flex:1"></span>
  <select id="cams" aria-label="Camera"></select>
  <button id="toggle">Start</button>
</header>
<div class="wrap">
  <div class="stage"><video id="v" playsinline muted></video><canvas id="c"></canvas></div>
  <div class="row">
    <span>people <b id="people">–</b></span><span>PPE <b id="ppe">–</b></span>
    <span>violations <b id="viol">–</b></span><span>poses <b id="poses">–</b></span>
    <span>inference <b id="ms">–</b> ms</span><span>dropped <b id="drop">0</b></span>
  </div>
  <p class="msg" id="msg">Pick a camera and press Start. Your browser will ask for permission.</p>
</div>
<script>
const $ = id => document.getElementById(id);
const SKELETON = [[5,7],[7,9],[6,8],[8,10],[5,6],[5,11],[6,12],[11,12],[11,13],[13,15],[12,14],
                  [14,16],[0,1],[0,2],[1,3],[2,4],[0,5],[0,6]];
const FPS = 9, QUALITY = 0.6, KP = 0.3;
let stream = null, socket = null, timer = null, inFlight = false;

function say(text, bad){ const m = $('msg'); m.textContent = text; m.className = 'msg' + (bad ? ' bad' : ''); }

async function listCameras(){
  if(!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  const devices = await navigator.mediaDevices.enumerateDevices();
  const cams = devices.filter(d => d.kind === 'videoinput');
  $('cams').innerHTML = cams.length
    ? cams.map((d,i) => `<option value="${d.deviceId}">${d.label || 'Camera ' + (i+1)}</option>`).join('')
      + '<option value="user">Front camera (mobile)</option><option value="environment">Rear camera (mobile)</option>'
    : '<option value="environment">Rear camera</option><option value="user">Front camera</option>';
}

function constraints(){
  const v = $('cams').value;
  const size = { width:{ideal:1280}, height:{ideal:720} };
  if(v === 'user' || v === 'environment') return { video:{ facingMode:{ideal:v}, ...size }, audio:false };
  return { video: v ? { deviceId:{exact:v}, ...size } : size, audio:false };
}

async function start(){
  if(!window.isSecureContext){
    say('Camera access needs a secure context: open this page over HTTPS, or via localhost.', true);
    return;
  }
  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    say('This browser does not expose getUserMedia, so it cannot share a camera.', true); return;
  }
  try{
    stream = await navigator.mediaDevices.getUserMedia(constraints());
  }catch(err){
    const n = err && err.name;
    say(n === 'NotAllowedError' ? 'Camera permission was denied — allow it in the address bar and retry.'
      : n === 'NotFoundError' ? 'No camera was found on this device.'
      : n === 'NotReadableError' ? 'The camera is already in use by another application.'
      : 'Could not open the camera: ' + (err && err.message || err), true);
    return;
  }
  $('v').srcObject = stream;
  await $('v').play();
  await listCameras();                       // labels are only exposed after permission
  connect();
  $('toggle').textContent = 'Stop';
}

function connect(){
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(scheme + '://' + location.host + '/ws/video');
  socket.onopen = () => {
    $('dot').style.background = 'var(--ok)';
    say('Streaming to the edge detector at ~' + FPS + ' fps.');
    timer = setInterval(send, Math.round(1000 / FPS));
  };
  socket.onclose = () => { $('dot').style.background = 'var(--hi)';
    if(timer){ clearInterval(timer); timer = null; }
    if(stream) say('The detector socket closed. Press Stop then Start to reconnect.', true); };
  socket.onerror = () => say('The detector socket could not be reached at /ws/video.', true);
  socket.onmessage = ev => {
    inFlight = false;
    const data = JSON.parse(ev.data);
    if(data.error){ say(data.error, true); return; }
    draw(data);
  };
}

const off = document.createElement('canvas');
function send(){
  const video = $('v');
  if(inFlight || !socket || socket.readyState !== WebSocket.OPEN || !video.videoWidth) return;
  off.width = video.videoWidth; off.height = video.videoHeight;
  off.getContext('2d').drawImage(video, 0, 0);
  off.toBlob(blob => {
    if(!blob || !socket || socket.readyState !== WebSocket.OPEN) return;
    inFlight = true;                                  // one frame in flight: never flood the edge
    blob.arrayBuffer().then(buf => socket.send(buf));
  }, 'image/jpeg', QUALITY);
}

function draw(data){
  $('people').textContent = data.counts.people; $('ppe').textContent = data.counts.ppe_items;
  $('viol').textContent = data.counts.violations; $('poses').textContent = data.counts.poses;
  $('ms').textContent = data.inference_ms; $('drop').textContent = data.dropped ?? 0;

  const c = $('c'), ctx = c.getContext('2d');
  c.width = data.frame.width; c.height = data.frame.height;
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.lineWidth = Math.max(2, c.width / 480); ctx.font = Math.round(c.width / 46) + 'px sans-serif';
  for(const d of data.detections){
    const [x1,y1,x2,y2] = d.box, color = d.is_violation ? '#ff4d4d' : '#4ade80';
    ctx.strokeStyle = color; ctx.strokeRect(x1, y1, x2-x1, y2-y1);
    const text = d.class + (d.confidence != null ? ' ' + d.confidence.toFixed(2) : '');
    ctx.fillStyle = 'rgba(0,0,0,.65)';
    ctx.fillRect(x1, Math.max(0, y1 - 18), ctx.measureText(text).width + 8, 18);
    ctx.fillStyle = color; ctx.fillText(text, x1 + 4, Math.max(12, y1 - 5));
  }
  const flagged = new Set(data.hazards.map(h => h.track_id));
  for(const pose of data.poses){
    const k = pose.keypoints, color = flagged.has(pose.track_id) ? '#ff4d4d' : '#5b9dff';
    ctx.strokeStyle = color; ctx.fillStyle = color;
    for(const [a,b] of SKELETON){
      if(k[a][2] < KP || k[b][2] < KP) continue;
      ctx.beginPath(); ctx.moveTo(k[a][0], k[a][1]); ctx.lineTo(k[b][0], k[b][1]); ctx.stroke();
    }
    for(const p of k){ if(p[2] >= KP){ ctx.beginPath(); ctx.arc(p[0], p[1], ctx.lineWidth, 0, 6.3); ctx.fill(); } }
  }
}

function stop(){
  if(timer){ clearInterval(timer); timer = null; }
  if(socket){ socket.onclose = null; socket.close(); socket = null; }
  if(stream){ stream.getTracks().forEach(t => t.stop()); stream = null; }
  $('v').srcObject = null; inFlight = false;
  $('dot').style.background = 'var(--dim)';
  $('c').getContext('2d').clearRect(0, 0, $('c').width, $('c').height);
  $('toggle').textContent = 'Start'; say('Stopped. The camera has been released.');
}

$('toggle').onclick = () => (stream ? stop() : start());
window.addEventListener('pagehide', stop);
listCameras().catch(() => {});
</script>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(run_gui())
