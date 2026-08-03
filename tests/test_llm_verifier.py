"""LLM verifier — parse-logic + fail-open behaviour (no Ollama needed)."""

from __future__ import annotations

from fieldpilot.llm.verifier import LLMVerifier


def _alert(event_type="crack", severity="high", payload=None):
    return {
        "alert_id": "a1", "dedup_key": "k", "event_type": event_type,
        "worker_id": "w-1", "camera_id": "cam-1", "zone": "zone-a",
        "severity": severity, "state": "NEW", "hit_count": 3,
        "confidence": 0.92, "message": "severe rotation crack",
        "payload": payload or {"defect": "Severerotation", "severity_score": 0.91},
    }


def test_parse_confirmed():
    v = LLMVerifier._parse(
        '{"confirmed": true, "confidence": 0.88, "reasoning": "crack visible", "severity": "critical"}',
        "llama3.2:3b",
    )
    assert v.confirmed is True and v.confidence == 0.88
    assert v.reasoning == "crack visible" and v.severity == "critical"
    assert v.llm_used and v.model == "llama3.2:3b"


def test_parse_rejected():
    v = LLMVerifier._parse(
        'The defect is minor. {"confirmed": false, "confidence": 0.7, "reasoning": "not structural"}',
        "llama3.2:3b",
    )
    assert v.confirmed is False


def test_parse_unparseable_fails_open():
    v = LLMVerifier._parse("sorry, can't help", "llama3.2:3b")
    assert v.confirmed is True  # fail-open on garbage
    assert v.llm_used is True


async def test_disabled_auto_confirms():
    v = LLMVerifier(enabled=False)
    verdict = await v.verify(_alert())
    assert verdict.confirmed is True and verdict.llm_used is False


async def test_no_ollama_fail_open():
    # point at a dead port → must auto-confirm rather than block
    v = LLMVerifier(ollama_host="http://127.0.0.1:59999", model="llama3.2:3b", timeout_s=2)
    verdict = await v.verify(_alert())
    assert verdict.confirmed is True and verdict.llm_used is False
    assert "unavailable" in verdict.reasoning.lower()


def test_prompt_contains_all_relevant_data():
    v = LLMVerifier()
    p = v._build_prompt(_alert(payload={"defect": "Severerotation", "severity_score": 0.91, "message": "severe crack"}))
    for token in ("Severerotation", "0.91", "severe crack", "crack", "high", "0.92", "w-1", "zone-a"):
        assert token in p, f"prompt missing {token!r}"
    assert "JSON" in p  # asks for structured reply

# --------------------------------------------------------------- suppression ceiling


class _AlwaysRejects:
    """A verifier stand-in that rejects everything, like a weak local model can."""

    model = "stub"

    def __init__(self, vision: bool = True) -> None:
        self.vision = vision

    async def verify(self, alert):
        from fieldpilot.llm.verifier import Verdict

        return Verdict(False, 0.2, "looks like a false positive to me", llm_used=True)


def _orchestrator(vision: bool = True, **kw):
    from fieldpilot.backend.service import Orchestrator

    return Orchestrator(
        bus=None, events=None, store=None, triggers=None, rules=None,
        notifications=None, verifier=_AlwaysRejects(vision=vision), **kw,
    )


def test_llm_may_suppress_only_up_to_the_configured_ceiling():
    orch = _orchestrator(suppress_max_severity="medium")
    assert orch._may_suppress("low") is True
    assert orch._may_suppress("medium") is True
    # a weak model must NOT be able to bin a serious hazard on its own judgement
    assert orch._may_suppress("high") is False
    assert orch._may_suppress("critical") is False


def test_ceiling_is_configurable_in_both_directions():
    assert _orchestrator(suppress_max_severity="low")._may_suppress("medium") is False
    assert _orchestrator(suppress_max_severity="critical")._may_suppress("critical") is True


def test_unknown_severity_is_treated_as_medium():
    orch = _orchestrator(suppress_max_severity="medium")
    assert orch._may_suppress("weird") is True
    assert orch._may_suppress("CRITICAL") is False, "comparison must be case-insensitive"


def test_a_verifier_without_vision_may_never_suppress():
    """A text-only model cannot judge imagery, so its rejections must not bin anything.

    Measured on llama3.2:3b with vision off: 14 of 19 rejections cited visual evidence the
    model was never given. Those alerts must survive as annotated-but-live.
    """

    orch = _orchestrator(vision=False, suppress_max_severity="critical")
    for severity in ("low", "medium", "high", "critical"):
        assert orch._may_suppress(severity) is False


def test_with_no_verifier_at_all_the_ceiling_still_applies():
    from fieldpilot.backend.service import Orchestrator

    orch = Orchestrator(bus=None, events=None, store=None, triggers=None, rules=None,
                        notifications=None, verifier=None, suppress_max_severity="medium")
    assert orch._may_suppress("medium") is True
    assert orch._may_suppress("critical") is False
