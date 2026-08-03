"""Text embeddings via Ollama, with a deterministic offline fallback.

Ollama (`nomic-embed-text`, 768-d) is the real path. When it is unreachable the embedder falls
back to a deterministic hashed bag-of-words vector so ingest and retrieval still *function*
offline and in tests — but that fallback is **lexical, not semantic**, and every consumer is told
so via `Embedder.degraded`, because silently serving worse retrieval as if it were the real thing
is exactly the failure mode the RAG design is meant to avoid.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.reasoning.embeddings")

_WORD = re.compile(r"[a-z0-9]+")


class Embedder:
    def __init__(
        self,
        *,
        ollama_host: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        dim: int = 768,
        timeout_s: float = 30.0,
        allow_fallback: bool = True,
    ) -> None:
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model
        self.dim = dim
        self.timeout_s = timeout_s
        self.allow_fallback = allow_fallback
        self.degraded = False          # True once we have served a fallback vector
        self._warned = False

    # -- public ---------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        vec = await self._ollama(text)
        if vec is not None:
            return vec
        if not self.allow_fallback:
            raise RuntimeError(
                f"embedding model {self.model!r} unavailable at {self.ollama_host} "
                "and fallback is disabled"
            )
        if not self._warned:
            log.warning(
                "Ollama embeddings unavailable at %s — falling back to DETERMINISTIC LEXICAL "
                "vectors. Retrieval will be keyword-ish, not semantic. Start Ollama and "
                "`ollama pull %s` for real embeddings.",
                self.ollama_host, self.model,
            )
            self._warned = True
        self.degraded = True
        return self._lexical(text)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    async def probe(self) -> dict[str, Any]:
        """Report whether the real embedding backend is reachable (for /health)."""

        vec = await self._ollama("probe")
        ok = vec is not None
        return {
            "backend": "ollama" if ok else "lexical-fallback",
            "model": self.model,
            "host": self.ollama_host,
            "dim": len(vec) if vec else self.dim,
            "semantic": ok,
        }

    # -- backends -------------------------------------------------------------

    async def _ollama(self, text: str) -> list[float] | None:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.ollama_host}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                resp.raise_for_status()
                vec = resp.json().get("embedding")
            if isinstance(vec, list) and vec:
                self.dim = len(vec)
                return [float(v) for v in vec]
            return None
        except Exception:  # noqa: BLE001 — any failure means "use the fallback"
            return None

    def _lexical(self, text: str) -> list[float]:
        """Hashed bag-of-words, L2-normalised. Deterministic across processes and runs."""

        vec = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            h = hashlib.blake2b(word.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec
