"""Draft Requests for Information from measured spec deviations.

A measurement event says "rebar spacing is 27.5 mm where the spec expects 20 mm". This module
retrieves the governing clause for **that project and zone** out of the blueprint index, asks the
local LLM to draft the RFI grounded in that clause, and files it as `pending_review`. A human
approves or rejects it — the LLM never files an RFI on its own authority.

Grounding is enforced structurally, not by prompt politeness:
  * retrieval is zone-`must`-filtered, so the cited clause cannot come from another zone;
  * the citation string is taken from the retrieved chunk's metadata, never from LLM output,
    so a hallucinated clause number cannot reach the document;
  * with nothing retrieved, the RFI is filed with `grounded: false` and a template body that
    says so, instead of an invented citation.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.reasoning.rag import BlueprintIndex, Chunk

log = get_logger("fieldpilot.reasoning.rfi")

RFI_STATUSES = ("pending_review", "approved", "rejected")


@dataclass
class DraftedRFI:
    rfi_id: str
    title: str
    summary: str
    body: str
    priority: str
    zone: str | None
    project_id: str
    status: str
    grounded: bool
    citations: list[dict[str, Any]]
    llm_used: bool
    event_id: str | None = None
    created_at: float = 0.0

    def to_record(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Shape for `PlatformStore.save_rfi` — review fields are first-class columns."""

        return {
            "rfi_id": self.rfi_id,
            "event_id": self.event_id,
            "title": self.title[:200],
            "summary": self.summary,
            "priority": self.priority,
            "zone": self.zone,
            "status": self.status,
            "citation": self.citations[0]["citation"] if self.citations else None,
            "body": self.body,
            "created_at": self.created_at or time.time(),
            "payload": {
                "project_id": self.project_id,
                "grounded": self.grounded,
                "citations": self.citations,
                "llm_used": self.llm_used,
                **(extra or {}),
            },
        }


class RFIDrafter:
    def __init__(
        self,
        index: BlueprintIndex | None,
        *,
        ollama_host: str = "http://localhost:11434",
        model: str = "llama3.2:3b",
        timeout_s: float = 30.0,
        enabled: bool = True,
    ) -> None:
        self.index = index
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.enabled = enabled

    async def draft(
        self,
        *,
        event: dict[str, Any],
        message: str = "",
        priority: str = "normal",
        project_id: str = "default",
    ) -> DraftedRFI:
        """Retrieve the governing clause, draft, and return an unfiled RFI."""

        payload = event.get("payload") or {}
        zone = event.get("zone")
        element = str(payload.get("element") or payload.get("defect") or "site condition")
        query = self._build_query(element, payload, message)

        chunks: list[Chunk] = []
        if self.index is not None and self.index.available:
            chunks = await self.index.search(
                query, project_id=project_id, zone=zone, category=None, top_k=3
            )

        citations = [
            {"citation": c.citation(), "clause": c.clause, "source": c.source,
             "page": c.page, "zone": c.zone, "score": c.score, "text": c.text[:600]}
            for c in chunks
        ]
        grounded = bool(citations)

        body, llm_used = await self._compose(
            element=element, payload=payload, message=message, zone=zone, chunks=chunks
        )
        deviation = payload.get("deviation_mm")
        title = (
            f"RFI — {element.replace('_', ' ')} deviation"
            + (f" of {deviation} mm" if deviation is not None else "")
            + (f" in {zone}" if zone else "")
        )
        return DraftedRFI(
            rfi_id=uuid.uuid4().hex,
            title=title,
            summary=(message or body.split("\n\n")[0])[:500],
            body=body,
            priority=priority,
            zone=zone,
            project_id=project_id,
            status="pending_review",
            grounded=grounded,
            citations=citations,
            llm_used=llm_used,
            event_id=event.get("event_id"),
            created_at=time.time(),
        )

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _build_query(element: str, payload: dict[str, Any], message: str) -> str:
        bits = [element.replace("_", " ")]
        for key in ("measured_mm", "expected_mm", "deviation_mm", "tolerance_mm"):
            if payload.get(key) is not None:
                bits.append(f"{key.replace('_', ' ')} {payload[key]}")
        if message:
            bits.append(message)
        bits.append("specification tolerance requirement")
        return " ".join(str(b) for b in bits)

    async def _compose(
        self, *, element: str, payload: dict[str, Any], message: str,
        zone: str | None, chunks: list[Chunk],
    ) -> tuple[str, bool]:
        """Ask the LLM for the RFI narrative; fall back to a deterministic template."""

        template = self._template(element, payload, message, zone, chunks)
        if not self.enabled:
            return template, False

        context = "\n\n".join(
            f"[{i + 1}] {c.citation()}\n{c.text}" for i, c in enumerate(chunks)
        ) or "(no specification text retrieved for this zone)"

        prompt = (
            "You are a construction project engineer drafting a Request For Information (RFI).\n"
            "Write a concise, professional RFI body (120-180 words) about the measured deviation "
            "below. Reference the specification extracts by their [n] markers where relevant. "
            "State the observed condition, why it needs clarification, and the specific question "
            "for the design team. Do NOT invent clause numbers, drawing numbers, or dates — use "
            "only what appears in the extracts.\n\n"
            f"Observed element: {element}\n"
            f"Zone: {zone or 'unspecified'}\n"
            f"Measurements: {json.dumps({k: v for k, v in payload.items() if _is_scalar(v)})}\n"
            f"Detector note: {message or 'n/a'}\n\n"
            f"Specification extracts:\n{context}\n\n"
            "Respond with the RFI body text only — no preamble, no headings, no JSON."
        )
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.3, "num_predict": 400}},
                )
                resp.raise_for_status()
                text = (resp.json().get("response") or "").strip()
        except Exception:  # noqa: BLE001 — drafting must never block the pipeline
            log.warning("RFI LLM draft failed — using deterministic template")
            return template, False

        text = _strip_fences(text)
        if len(text) < 80:
            return template, False
        # The measured numbers are stated deterministically from the event, above the LLM prose.
        # Small local models do misstate arithmetic (e.g. calling a 7.5 mm deviation "within
        # ±5 mm"), and an RFI that misreports its own measurement is worse than a terse one.
        facts = self._facts_block(payload)
        citation_block = self._citation_block(chunks)
        parts = [p for p in (facts, text, citation_block) if p]
        return "\n\n".join(parts), True

    @staticmethod
    def _facts_block(payload: dict[str, Any]) -> str:
        rows = [
            ("Measured", payload.get("measured_mm")),
            ("Specified", payload.get("expected_mm")),
            ("Deviation", payload.get("deviation_mm")),
            ("Permitted tolerance", payload.get("tolerance_mm")),
        ]
        present = [(k, v) for k, v in rows if v is not None]
        if not present:
            return ""
        line = "; ".join(f"{k}: {v} mm" for k, v in present)
        verdict = ""
        dev, tol = payload.get("deviation_mm"), payload.get("tolerance_mm")
        if isinstance(dev, (int, float)) and isinstance(tol, (int, float)):
            verdict = (" — OUTSIDE permitted tolerance."
                       if abs(dev) > abs(tol) else " — within permitted tolerance.")
        return f"Recorded measurements ({line}){verdict}"

    @staticmethod
    def _citation_block(chunks: list[Chunk]) -> str:
        if not chunks:
            return ("References: none — no specification text is indexed for this zone. "
                    "This RFI is UNGROUNDED and must be checked against the drawings manually.")
        lines = ["References:"]
        lines += [f"  [{i + 1}] {c.citation()}" for i, c in enumerate(chunks)]
        return "\n".join(lines)

    def _template(
        self, element: str, payload: dict[str, Any], message: str,
        zone: str | None, chunks: list[Chunk],
    ) -> str:
        measured = payload.get("measured_mm")
        expected = payload.get("expected_mm")
        deviation = payload.get("deviation_mm")
        lines = [
            f"During automated site monitoring, {element.replace('_', ' ')} was measured in "
            f"{zone or 'an unspecified zone'} and appears to deviate from the issued "
            "specification.",
        ]
        detail = []
        if measured is not None:
            detail.append(f"measured {measured} mm")
        if expected is not None:
            detail.append(f"specified {expected} mm")
        if deviation is not None:
            detail.append(f"deviation {deviation} mm")
        if detail:
            lines.append("Recorded values: " + ", ".join(detail) + ".")
        if message:
            lines.append(f"Detector note: {message}")
        lines.append(
            "Please confirm whether the as-built condition is acceptable, or issue direction for "
            "remedial work. Work in the affected area is proceeding at risk until clarified."
        )
        return "\n\n".join(lines) + "\n\n" + self._citation_block(chunks)


def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    return re.sub(r"\n?```$", "", text).strip()
