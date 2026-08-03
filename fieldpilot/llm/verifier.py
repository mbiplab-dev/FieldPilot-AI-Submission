"""LLM verifier — calls a local Ollama model to confirm/reject each alert.

Every alert carries: the detector's event type, severity, confidence, the defect/message,
a severity_score, and the bbox of what was flagged. The verifier builds a prompt from that
metadata and (when a vision model is configured and the captured image exists) attaches the
annotated JPEG so the LLM can SEE the flagged region. The LLM replies with a JSON verdict:

    {"confirmed": true|false, "confidence": 0..1, "reasoning": "...", "severity": "..."}

Fail-open: if Ollama is unreachable or the model isn't pulled, the alert is auto-confirmed
with `llm_used=False` so the safety loop never blocks on infra.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.llm.verifier")


@dataclass
class Verdict:
    confirmed: bool
    confidence: float
    reasoning: str
    severity: str | None = None
    llm_used: bool = True
    model: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class LLMVerifier:
    def __init__(
        self,
        *,
        ollama_host: str = "http://localhost:11434",
        model: str = "llama3.2:3b",
        vision: bool = False,
        enabled: bool = True,
        timeout_s: float = 12.0,
        images_dir: str = "data/alerts",
    ) -> None:
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model
        self.vision = bool(vision)
        self.enabled = bool(enabled)
        self.timeout_s = float(timeout_s)
        self.images_dir = Path(images_dir)
        self._available: bool | None = None  # cache the tags check
        self._available_model: str | None = None

    async def verify(self, alert: dict) -> Verdict:
        """Return the LLM verdict for an alert dict. Fail-open on any infra issue."""

        if not self.enabled:
            return Verdict(True, 0.0, "LLM disabled — auto-confirmed", llm_used=False)

        prompt = self._build_prompt(alert)
        image_b64 = self._load_image(alert)

        # resolve a usable model
        model = await self._resolve_model(want_vision=image_b64 is not None)
        if model is None:
            return Verdict(True, 0.0, "LLM unavailable — auto-confirmed", llm_used=False)

        try:
            import httpx

            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 200},
            }
            if image_b64 is not None:
                payload["images"] = [image_b64]
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(f"{self.ollama_host}/api/generate", json=payload)
                r.raise_for_status()
                text = (r.json().get("response") or "").strip()
        except Exception:  # noqa: BLE001 — never block the safety loop on the LLM
            log.warning("LLM verifier call failed — auto-confirming (fail-open)")
            return Verdict(True, 0.0, "LLM call failed — auto-confirmed",
                           llm_used=False, model=model)

        verdict = self._parse(text, model)
        log.info("LLM verdict: confirmed=%s conf=%.2f — %s",
                 verdict.confirmed, verdict.confidence, verdict.reasoning[:120])
        return verdict

    # ------------------------------------------------------------------ helpers

    def _build_prompt(self, alert: dict) -> str:
        p = alert.get("payload") or {}
        sev = alert.get("severity", "medium")
        etype = alert.get("event_type", "unknown")
        conf = alert.get("confidence", 0.0)
        score = p.get("severity_score")
        defect = p.get("defect") or p.get("ppe_item") or etype
        msg = p.get("message") or alert.get("message") or ""
        worker = alert.get("worker_id") or "unknown"
        bbox = p.get("bbox") or alert.get("bbox")
        hits = alert.get("hit_count", 1)

        parts = [
            "You are a construction-site safety VERIFICATION AI.",
            "A downstream detector raised the following alert — decide if it is GENUINE",
            "(worth acting on) or likely a FALSE POSITIVE, given the metadata"
            + (" and the attached image of the flagged region." if self.vision else ".") + "\n",
            f"event_type: {etype}",
            f"severity: {sev}",
            f"detector_confidence: {conf:.2f}",
            f"deduplicated_hits: {hits}",
        ]
        if score is not None:
            parts.append(f"severity_score: {score}")
        parts += [
            f"defect_or_issue: {defect}",
            f"worker: {worker}",
            f"zone: {alert.get('zone') or 'unknown'}",
            f"camera: {alert.get('camera_id') or 'unknown'}",
            f"message: {msg}",
        ]
        if bbox:
            parts.append(f"bbox (x1,y1,x2,y2): {bbox}")
        parts += [
            "\nRespond ONLY with compact JSON, no prose:",
            '{"confirmed": true|false, "confidence": 0.0-1.0, "reasoning": "one short sentence", "severity": "low|medium|high|critical"}',
        ]
        return "\n".join(parts)

    def _load_image(self, alert: dict) -> str | None:
        """Resolve the alert's image_url to a base64 blob, if vision + file exists."""

        if not self.vision:
            return None
        url = (alert.get("payload") or {}).get("image_url") or alert.get("image_url")
        if not url:
            return None
        # image_url is like "/img/<file>.jpg" — resolve to the local file
        fname = url.split("/")[-1]
        path = self.images_dir / fname
        if not path.exists():
            return None
        try:
            return base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception:  # noqa: BLE001
            return None

    async def _resolve_model(self, want_vision: bool) -> str | None:
        """Pick a model that is actually pulled. Falls back from vision→text→none."""

        if self._available is None:
            self._available = await self._model_available(self.model)
            if self._available:
                self._available_model = self.model
            else:
                # try a text fallback
                for fb in ("llama3.2:3b", "llama3.2", "qwen2.5:3b", "phi3:mini"):
                    if await self._model_available(fb):
                        self._available_model = fb
                        self._available = True
                        self.vision = False  # can't do vision with the text fallback
                        break
                if not self._available:
                    self._available_model = None
        if want_vision and not self.vision:
            return None  # caller wanted vision but only text model available → text is fine
        return self._available_model

    async def _model_available(self, model: str) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.ollama_host}/api/tags")
                r.raise_for_status()
                names = [m.get("name", "") for m in r.json().get("models", [])]
            return any(model in n or n.startswith(model.split(":")[0]) for n in names)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _parse(text: str, model: str) -> Verdict:
        """Extract the JSON verdict from the LLM's (possibly chatty) response."""

        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m:
            try:
                d = json.loads(m.group(0))
                return Verdict(
                    confirmed=bool(d.get("confirmed", True)),
                    confidence=float(d.get("confidence", 0.8)),
                    reasoning=str(d.get("reasoning", ""))[:300],
                    severity=d.get("severity"),
                    llm_used=True,
                    model=model,
                )
            except (ValueError, TypeError):
                pass
        # unparseable → fail open (confirm) but record the raw text
        return Verdict(True, 0.7, f"unparseable LLM reply: {text[:160]}", llm_used=True, model=model)