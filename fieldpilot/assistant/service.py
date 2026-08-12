"""A worker-invoked assistant with a deliberately narrow authority boundary.

Gemma may describe an image, explain a site specification, or choose which UI tool is useful.
It may not calculate a physical dimension from an image, suppress a detector alert, or file a
hazard report without a worker confirmation. Those operations stay in deterministic code.
"""

from __future__ import annotations

import asyncio
import base64
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.assistant")

_INTENTS = frozenset({"general", "identify", "specification", "measure", "report", "emergency"})
_HAZARDS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("fire", ("fire", "flame", "smoke"), "critical"),
    ("gas", ("gas", "fumes", "chemical leak"), "critical"),
    ("fall", ("fell", "fallen", "fall", "collapsed", "unconscious"), "critical"),
    ("proximity", ("too close", "near machine", "near vehicle", "struck by"), "high"),
    ("ppe", ("no helmet", "no hardhat", "no vest", "missing ppe"), "high"),
    ("inspection", ("crack", "spill", "blocked exit", "unsafe", "hazard"), "high"),
)


class AssistantError(ValueError):
    """The worker's assistant request is invalid."""


class MeasurementError(ValueError):
    """A calibrated point measurement cannot be computed safely."""


@dataclass
class AssistantReply:
    answer: str
    intent: str = "general"
    confidence: float = 0.0
    model: str | None = None
    degraded: bool = False
    requires_confirmation: bool = False
    action: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    safety_note: str = (
        "Advisory only. Stop work and contact the site manager when conditions are uncertain."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _point(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise MeasurementError(f"{name} must contain exactly two coordinates")
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise MeasurementError(f"{name} contains a non-numeric coordinate") from exc
    if not math.isfinite(x) or not math.isfinite(y):
        raise MeasurementError(f"{name} coordinates must be finite")
    return x, y


def measure_from_points(
    *,
    reference_points: Any,
    measurement_points: Any,
    reference_mm: float,
    spec_mm: float | None = None,
    tolerance_mm: float = 5.0,
    image_size: Any = None,
) -> dict[str, Any]:
    """Measure a coplanar segment against a worker-marked reference segment.

    This is intentionally geometry, not VLM output. Both segments must lie on the same plane and
    at approximately the same depth; the response carries that limitation into the UI.
    """

    if not isinstance(reference_points, (list, tuple)) or len(reference_points) != 2:
        raise MeasurementError("reference_points must contain two points")
    if not isinstance(measurement_points, (list, tuple)) or len(measurement_points) != 2:
        raise MeasurementError("measurement_points must contain two points")
    r1, r2 = _point(reference_points[0], "reference point"), _point(
        reference_points[1], "reference point"
    )
    m1, m2 = _point(measurement_points[0], "measurement point"), _point(
        measurement_points[1], "measurement point"
    )
    try:
        known_mm = float(reference_mm)
        tolerance = float(tolerance_mm)
    except (TypeError, ValueError) as exc:
        raise MeasurementError("reference and tolerance must be numeric") from exc
    if not math.isfinite(known_mm) or known_mm <= 0:
        raise MeasurementError("reference_mm must be greater than zero")
    if not math.isfinite(tolerance) or tolerance < 0:
        raise MeasurementError("tolerance_mm cannot be negative")

    points = (r1, r2, m1, m2)
    if image_size is not None:
        width, height = _point(image_size, "image_size")
        if width <= 0 or height <= 0:
            raise MeasurementError("image_size must be positive")
        if any(x < 0 or y < 0 or x > width or y > height for x, y in points):
            raise MeasurementError("all points must lie inside the image")

    reference_px = math.dist(r1, r2)
    measured_px = math.dist(m1, m2)
    if reference_px < 2:
        raise MeasurementError("reference points are too close together")
    if measured_px < 1:
        raise MeasurementError("measurement points are too close together")

    px_per_mm = reference_px / known_mm
    measured_mm = measured_px / px_per_mm
    result: dict[str, Any] = {
        "reference_px": round(reference_px, 2),
        "measurement_px": round(measured_px, 2),
        "px_per_mm": round(px_per_mm, 5),
        "measured_mm": round(measured_mm, 2),
        "method": "coplanar_reference",
        "limitations": (
            "Reference and target must be on the same plane and depth. This is an advisory "
            "camera measurement; verify critical dimensions with a calibrated physical tool."
        ),
    }
    if spec_mm is not None:
        try:
            spec = float(spec_mm)
        except (TypeError, ValueError) as exc:
            raise MeasurementError("spec_mm must be numeric") from exc
        if not math.isfinite(spec) or spec <= 0:
            raise MeasurementError("spec_mm must be greater than zero")
        deviation = measured_mm - spec
        result.update({
            "spec_mm": round(spec, 2),
            "tolerance_mm": round(tolerance, 2),
            "deviation_mm": round(deviation, 2),
            "within_tolerance": abs(deviation) <= tolerance,
        })
    return result


class AssistantService:
    """Route a spoken request to Gemma and deterministic FieldPilot tools."""

    def __init__(
        self,
        *,
        ollama_host: str = "http://localhost:11434",
        model: str = "gemma4:e4b-it-qat",
        enabled: bool = True,
        timeout_s: float = 45.0,
        index: Any = None,
        project_id: str = "default",
    ) -> None:
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.index = index
        self.project_id = project_id
        # A 6 GB laptop GPU cannot comfortably execute Gemma and multiple vision requests at once.
        self._inference_lock = asyncio.Lock()

    async def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "available": False, "model": self.model,
                    "reason": "assistant disabled by configuration"}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.ollama_host}/api/tags")
                response.raise_for_status()
                names = {str(m.get("name")) for m in response.json().get("models", [])}
            available = self.model in names or any(
                name.split(":", 1)[0] == self.model.split(":", 1)[0] for name in names
            )
            return {"enabled": True, "available": available, "model": self.model,
                    "reason": None if available else "configured model is not installed"}
        except Exception as exc:  # noqa: BLE001 - status must describe degradation, not raise
            return {"enabled": True, "available": False, "model": self.model,
                    "reason": f"Ollama unavailable: {type(exc).__name__}"}

    async def ask(
        self,
        text: str,
        *,
        zone: str | None = None,
        worker_id: str | None = None,
        image_bytes: bytes | None = None,
    ) -> AssistantReply:
        request = str(text or "").strip()
        if not request:
            raise AssistantError("say or type a request first")
        if len(request) > 2000:
            raise AssistantError("assistant request is too long (2000 characters maximum)")

        routed = self._deterministic_route(request, image_bytes=bool(image_bytes))
        if routed is not None:
            return routed

        intent = self._intent(request, image_bytes=bool(image_bytes))
        chunks = await self._retrieve(request, zone=zone) if intent == "specification" else []
        citations = [
            {"citation": c.citation(), "source": c.source, "clause": c.clause,
             "page": c.page, "zone": c.zone, "score": c.score}
            for c in chunks
        ]
        if not self.enabled:
            return self._fallback(request, intent, bool(image_bytes), citations)

        try:
            answer = await self._generate(
                request, intent=intent, zone=zone, worker_id=worker_id,
                image_bytes=image_bytes, chunks=chunks,
            )
        except Exception as exc:  # noqa: BLE001 - the demo must degrade instead of hanging
            log.warning("assistant model unavailable: %s", exc)
            return self._fallback(request, intent, bool(image_bytes), citations)

        return AssistantReply(
            answer=answer,
            intent=intent,
            confidence=0.82,
            model=self.model,
            citations=citations,
        )

    @staticmethod
    def _intent(text: str, *, image_bytes: bool) -> str:
        lower = text.lower()
        if any(word in lower for word in ("spec", "drawing", "requirement", "allowed", "spacing")):
            return "specification"
        if image_bytes or any(word in lower for word in ("identify", "what is", "look at", "this tool")):
            return "identify"
        return "general"

    def _deterministic_route(self, text: str, *, image_bytes: bool) -> AssistantReply | None:
        lower = text.lower()
        if any(phrase in lower for phrase in ("help me", "emergency", "mayday", "sos")):
            return AssistantReply(
                answer=(
                    "Emergency request heard. Stop work, move away from immediate danger if you "
                    "can do so safely, and confirm to alert the site manager."
                ),
                intent="emergency",
                confidence=1.0,
                requires_confirmation=True,
                action={"type": "confirm_hazard_report", "event_type": "inspection",
                        "severity": "critical", "message": text[:500]},
            )
        if any(word in lower for word in ("measure", "distance", "length", "width", "spacing")):
            return AssistantReply(
                answer=(
                    "Open the measurement tool, mark both ends of a known reference, then mark "
                    "the two points to measure. Keep the reference and target on the same plane."
                ),
                intent="measure",
                confidence=0.98,
                action={"type": "open_measurement"},
            )
        report_words = ("report", "notify manager", "raise alert", "log hazard")
        if any(word in lower for word in report_words):
            event_type, severity = self._hazard_from_text(lower)
            return AssistantReply(
                answer=(
                    f"I prepared a {severity} {event_type} hazard report. Review it and confirm "
                    "before it is sent to the site manager."
                ),
                intent="report",
                confidence=0.96,
                requires_confirmation=True,
                action={"type": "confirm_hazard_report", "event_type": event_type,
                        "severity": severity, "message": text[:500]},
            )
        if not image_bytes and any(word in lower for word in ("identify", "what is this", "look at this")):
            return AssistantReply(
                answer="Attach or capture a clear photo so I can identify what you are looking at.",
                intent="identify",
                confidence=1.0,
                action={"type": "capture_photo"},
            )
        return None

    @staticmethod
    def _hazard_from_text(text: str) -> tuple[str, str]:
        for event_type, terms, severity in _HAZARDS:
            if any(term in text for term in terms):
                return event_type, severity
        return "inspection", "high"

    async def _retrieve(self, query: str, *, zone: str | None) -> list[Any]:
        if self.index is None or not getattr(self.index, "available", False):
            return []
        try:
            return await self.index.search(
                query, project_id=self.project_id, zone=zone, top_k=3,
            )
        except Exception:  # noqa: BLE001 - retrieval is optional in demo mode
            log.exception("assistant specification retrieval failed")
            return []

    async def _generate(
        self,
        request: str,
        *,
        intent: str,
        zone: str | None,
        worker_id: str | None,
        image_bytes: bytes | None,
        chunks: list[Any],
    ) -> str:
        import httpx

        context = "\n\n".join(
            f"[{i + 1}] {chunk.citation()}\n{chunk.text[:1600]}"
            for i, chunk in enumerate(chunks)
        ) or "No project specification was retrieved."
        prompt = (
            "You are FieldPilot, a construction worker's hands-free assistant. Give a direct, "
            "plain-language answer in at most three short sentences. You are advisory, not a "
            "safety authority. Never declare a scene safe. Never estimate dimensions from image "
            "pixels. Never invent a regulation, drawing, clause, object label, or measurement. "
            "If immediate danger is possible, begin with 'Stop work.' If visual evidence is "
            "unclear, say what clearer view is needed. Use specification extracts only when they "
            "actually answer the question and refer to them as [1], [2], or [3].\n\n"
            f"Intent: {intent}\nWorker: {worker_id or 'unknown'}\nZone: {zone or 'unspecified'}\n"
            f"Request: {request}\n\nSpecification extracts:\n{context}\n\nAnswer only:"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # Gemma 4's thinking channel can consume the whole short response budget before an
            # answer reaches `response`. The field assistant needs a fast spoken answer, not a
            # hidden reasoning trace, so disable thinking for this latency-sensitive path.
            "think": False,
            "options": {"temperature": 0.2, "num_predict": 220},
        }
        if image_bytes:
            payload["images"] = [base64.b64encode(image_bytes).decode("ascii")]
        async with self._inference_lock:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(f"{self.ollama_host}/api/generate", json=payload)
                response.raise_for_status()
                answer = str(response.json().get("response") or "").strip()
        # Gemma thinking variants can emit a thought channel. Never forward hidden analysis to TTS.
        answer = re.sub(r"<\|channel>thought.*?<channel\|>", "", answer, flags=re.DOTALL).strip()
        if len(answer) < 3:
            raise RuntimeError("Gemma returned an empty answer")
        return answer[:1600]

    @staticmethod
    def _fallback(
        request: str, intent: str, image_bytes: bool, citations: list[dict[str, Any]],
    ) -> AssistantReply:
        if intent == "identify" and image_bytes:
            answer = (
                "The visual assistant is unavailable, so I cannot identify this reliably. "
                "Keep clear of the object and ask the site manager to inspect the photo."
            )
        elif intent == "specification" and not citations:
            answer = (
                "No project specification is indexed for this request. Check the approved drawing "
                "or ask the site manager before continuing."
            )
        else:
            answer = (
                "The local assistant is unavailable. Stop work if this may be unsafe and contact "
                "the site manager."
            )
        return AssistantReply(
            answer=answer, intent=intent, confidence=0.0, model=None, degraded=True,
            citations=citations,
        )
