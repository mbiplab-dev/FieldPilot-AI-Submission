"""Async video sources.

Capture runs on a dedicated thread (OpenCV `VideoCapture` is blocking) and pushes frames into a
bounded queue with **drop-oldest** semantics — under inference backpressure we always keep the most
recent frame rather than stalling the camera or growing latency. The async `frames()` generator
yields to the pipeline without blocking the event loop.

Sources:
- `webcam`    — live `/dev/videoN`.
- `file`      — a video file, paced to `target_fps` (or as fast as possible when `pace=False`).
- `synthetic` — generated frames, so the full loop (and the 10-minute validation run) works with no
                camera and no assets.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time

import cv2
import numpy as np

from fieldpilot.core.types import Frame
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.video")
_SENTINEL = object()


class VideoSource:
    def __init__(
        self,
        kind: str = "webcam",
        *,
        webcam_index: int = 0,
        file_path: str | None = None,
        target_fps: int = 30,
        queue_maxsize: int = 4,
        pace: bool = True,
        max_frames: int | None = None,
    ):
        self.kind = kind
        self.webcam_index = webcam_index
        self.file_path = file_path
        self.target_fps = max(1, int(target_fps))
        self.pace = pace
        self.max_frames = max_frames
        self._q: queue.Queue = queue.Queue(maxsize=max(1, queue_maxsize))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dropped = 0

    # ---- lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._reader, name="video-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def dropped(self) -> int:
        return self._dropped

    # ---- capture thread ------------------------------------------------------------------------
    def _open(self):
        if self.kind == "webcam":
            cap = cv2.VideoCapture(self.webcam_index)
            cap.set(cv2.CAP_PROP_FPS, self.target_fps)
            return cap
        if self.kind == "file":
            if not self.file_path:
                raise ValueError("file source requires file_path")
            return cv2.VideoCapture(self.file_path)
        return None  # synthetic

    def _reader(self) -> None:
        cap = None
        try:
            cap = self._open()
            if self.kind != "synthetic" and (cap is None or not cap.isOpened()):
                log.error("could not open video source kind=%s (%s)", self.kind, self.file_path
                          or self.webcam_index)
                self._q.put(_SENTINEL)
                return

            frame_interval = 1.0 / self.target_fps
            idx = 0
            while not self._stop.is_set():
                loop_start = time.monotonic()
                if self.kind == "synthetic":
                    image = _synthetic_frame(idx)
                else:
                    ok, image = cap.read()
                    if not ok:
                        break  # end of file / camera gone
                self._offer(Frame(index=idx, ts_monotonic=time.monotonic(), image=image))
                idx += 1
                if self.max_frames is not None and idx >= self.max_frames:
                    break
                if self.pace:
                    elapsed = time.monotonic() - loop_start
                    sleep = frame_interval - elapsed
                    if sleep > 0:
                        time.sleep(sleep)
        finally:
            if cap is not None:
                cap.release()
            self._q.put(_SENTINEL)

    def _offer(self, frame: Frame) -> None:
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            try:
                self._q.get_nowait()   # drop the oldest, keep the freshest
                self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass

    # ---- async consumer ------------------------------------------------------------------------
    async def frames(self):
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            item = await loop.run_in_executor(None, self._q.get)
            if item is _SENTINEL:
                break
            yield item


def _synthetic_frame(idx: int, w: int = 640, h: int = 480) -> np.ndarray:
    """A cheap moving-gradient frame so the pipeline runs without a camera or assets."""

    img = np.zeros((h, w, 3), dtype=np.uint8)
    shift = (idx * 3) % 255
    base = np.linspace(0, 255, w, dtype=np.int32)
    img[:, :, 0] = ((base + shift) % 255).astype(np.uint8)
    img[:, :, 1] = 40
    cx = int((0.5 + 0.4 * np.sin(idx / 20.0)) * w)
    cy = int((0.5 + 0.3 * np.cos(idx / 25.0)) * h)
    cv2.circle(img, (cx, cy), 30, (0, 165, 255), -1)
    return img
