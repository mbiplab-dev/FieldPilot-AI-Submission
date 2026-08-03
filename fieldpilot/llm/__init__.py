"""LLM verification gate — every trigger hops through a small local LLM for a final verdict.

The trigger engine creates an alert; before any notification or rule action fires, the
orchestrator asks a locally-runnable LLM (Ollama) to look at the captured image + the
detector's metadata and confirm or reject the alert. This cuts false positives — the
detectors are noisy, the LLM is the arbiter.

Fails OPEN: if no LLM is available, the alert is auto-confirmed (never silently dropped).
"""

from fieldpilot.llm.verifier import LLMVerifier, Verdict

__all__ = ["LLMVerifier", "Verdict"]