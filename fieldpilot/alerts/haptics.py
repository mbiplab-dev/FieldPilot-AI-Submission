"""Haptic dispatch.

Severity-scaled vibration patterns are pushed to the paired mobile device (the compute gateway),
since the display-less glasses have no useful haptics of their own. When no mobile endpoint is
configured (the M1 default), the pattern is logged as a simulated buzz so the pipeline is fully
exercisable without a phone.
"""

from __future__ import annotations

import requests

from fieldpilot.core.types import Severity
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.haptics")


class HapticDispatcher:
    def __init__(self, cfg):
        h = cfg.section("alerts").get("haptics", {})
        self.enabled = bool(h.get("enabled", True))
        self.patterns: dict[str, list[int]] = h.get("patterns", {})
        self.endpoint = h.get("mobile_endpoint")

    def buzz(self, severity: Severity, hazard_type: str) -> None:
        if not self.enabled:
            return
        pattern = self.patterns.get(severity.value, self.patterns.get("low", [150]))
        payload = {"pattern_ms": pattern, "severity": severity.value, "hazard": hazard_type}
        if not self.endpoint:
            log.info("[haptic:simulated] %s → %s", severity.value, pattern)
            return
        try:
            requests.post(self.endpoint, json=payload, timeout=1.0)
        except requests.RequestException:
            # Never let a phone hiccup block a safety alert; the audio channel still fires.
            log.debug("haptic push failed to %s", self.endpoint, exc_info=True)
