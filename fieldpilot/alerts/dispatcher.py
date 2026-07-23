"""Alert dispatcher.

Turns a HazardEvent into the multimodal alert: a category earcon, a severity-scaled haptic buzz, and
a spoken explanation. Per-category cooldowns prevent alert storms. The blocking side-effects (audio
+ TTS) run on a small thread pool so the inference loop is never stalled; the detection→alert latency
(measured the instant an alert is admitted past cooldown) is returned for the <500 ms budget check.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from fieldpilot.alerts.earcons import EarconBank
from fieldpilot.alerts.haptics import HapticDispatcher
from fieldpilot.alerts.tts import TTS
from fieldpilot.core.types import HazardEvent
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.alerts")


@dataclass
class AlertRecord:
    event: HazardEvent
    admitted: bool           # False if suppressed by cooldown
    latency_ms: float = 0.0  # detection → alert admitted


class AlertDispatcher:
    def __init__(self, cfg):
        self.cooldowns: dict[str, float] = cfg.get("alerts.cooldown_s", {}) or {}
        self.earcons = EarconBank(cfg)
        self.tts = TTS(cfg)
        self.haptics = HapticDispatcher(cfg)
        self._last: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alert")
        self.dry_run = False  # when True, admission is measured but no audio/haptic is rendered

    def _cooldown_ok(self, category: str, now: float) -> bool:
        cd = float(self.cooldowns.get(category, 0.0))
        last = self._last.get(category, -1e9)
        if now - last < cd:
            return False
        self._last[category] = now
        return True

    def dispatch(self, event: HazardEvent) -> AlertRecord:
        now = time.monotonic()
        if not self._cooldown_ok(event.category(), now):
            return AlertRecord(event=event, admitted=False)

        latency_ms = (now - event.ts_monotonic) * 1000.0
        if not self.dry_run:
            log.warning(
                "ALERT [%s/%s] %s  (%.0f ms)",
                event.category(), event.severity.value, event.message, latency_ms,
            )
            self._pool.submit(self._render, event)
        return AlertRecord(event=event, admitted=True, latency_ms=latency_ms)

    def _render(self, event: HazardEvent) -> None:
        try:
            self.earcons.play(event.category())
            self.haptics.buzz(event.severity, event.category())
            self.tts.speak(event.message)
        except Exception:  # noqa: BLE001 — an alert must never crash the pipeline.
            log.debug("alert render failed", exc_info=True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
