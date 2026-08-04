"""A worker asks a question about what they are looking at, with a photo.

This is agents A5 (Voice/NLP) and A7 (Knowledge Retrieval) from docs/agents.md. The question
fans out two ways, deliberately:

  * to the **LLM**, for an immediate answer grounded in the site's own specification documents;
  * to the **site manager**, who is the authority and can correct or override it.

Both matter. An LLM answer alone would be unaccountable on a safety question; a manager alone
would be too slow to be useful while the worker is standing in front of the thing.

Two rules carried over from `reasoning/rfi.py`, for the same reasons:
  * retrieval is zone-filtered, so a clause governing another zone can never be quoted back;
  * citations are built from chunk METADATA, never from model output, so a hallucinated document
    name or clause number cannot reach the worker.

The LLM is never on the worker's critical path: `ask()` persists and returns immediately, and
`answer_with_llm()` runs afterwards. A worker holding a phone in the rain gets an instant
acknowledgement, and the answer arrives when it arrives.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.workforce.questions")

QUESTIONS_TABLE = TableSpec(
    "worker_questions",
    key="question_id",
    columns=(
        Column("worker_id", indexed=True),
        Column("zone", indexed=True),
        Column("text"),
        Column("image_path"),
        Column("status", indexed=True),        # pending | answered | closed
        Column("llm_answer"),
        Column("llm_grounded", "bool"),
        Column("llm_model"),
        Column("llm_error"),
        Column("manager_reply"),
        Column("manager_id"),
        Column("replied_at", "real"),
        Column("answered_at", "real"),
        Column("created_at", "real"),
    ),
)

STATUSES = ("pending", "answered", "closed")

#: how much retrieved specification text to hand the model
_TOP_K = 3
_MAX_TEXT = 2000


class QuestionError(ValueError):
    """A question could not be accepted as given."""


def resolve_image_path(path: str | None, allowed_dir: str | Path) -> str | None:
    """Validate that `path` names a real file inside `allowed_dir`.

    Rejects traversal (`../../etc/passwd`) and absolute paths pointing elsewhere. Returns the
    resolved path as a string, or None when no image was supplied.

    The endpoint writes the upload; this only checks it, so a crafted `image_path` in a request
    body cannot be used to read or attach a file from outside the upload directory.
    """

    if not path:
        return None
    root = Path(allowed_dir).resolve()
    candidate = (root / Path(path).name) if not Path(path).is_absolute() else Path(path)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise QuestionError(f"image path {path!r} is outside the upload directory")
    if not resolved.is_file():
        raise QuestionError(f"image path {path!r} does not exist")
    return str(resolved)


class QuestionService:
    """Persist worker questions, answer them from the spec corpus, and route them to a human."""

    def __init__(
        self,
        store: DocStore,
        *,
        index: Any = None,
        ollama_host: str = "http://localhost:11434",
        model: str = "llama3.2:3b",
        timeout_s: float = 30.0,
        project_id: str = "default",
        uploads_dir: str = "data/uploads",
        llm_enabled: bool = True,
    ) -> None:
        self._table = store.table(QUESTIONS_TABLE)
        self.index = index
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.project_id = project_id
        self.uploads_dir = uploads_dir
        self.llm_enabled = llm_enabled

    async def start(self) -> None:
        Path(self.uploads_dir).mkdir(parents=True, exist_ok=True)

    # -- worker side -----------------------------------------------------------

    async def ask(
        self,
        *,
        worker_id: str,
        text: str,
        zone: str | None = None,
        image_path: str | None = None,
    ) -> dict[str, Any]:
        """Record the question and return at once. Never blocks on the LLM."""

        question = str(text or "").strip()
        if not question:
            raise QuestionError("a question needs some text")
        if len(question) > 2000:
            raise QuestionError("question is too long (2000 characters maximum)")
        stored_image = resolve_image_path(image_path, self.uploads_dir)

        record = await self._table.put({
            "question_id": uuid.uuid4().hex,
            "worker_id": str(worker_id),
            "zone": zone,
            "text": question,
            "image_path": stored_image,
            "status": "pending",
            "llm_answer": None,
            "llm_grounded": None,
            "llm_model": None,
            "llm_error": None,
            "manager_reply": None,
            "manager_id": None,
            "replied_at": None,
            "answered_at": None,
            "created_at": time.time(),
            # undeclared keys ride in the payload column
            "citations": [],
        })
        log.info("question %s from %s in zone %s (image=%s)",
                 record["question_id"], worker_id, zone, bool(stored_image))
        return record

    # -- A7 retrieval + A5 answer ---------------------------------------------

    async def answer_with_llm(self, question_id: str) -> dict[str, Any] | None:
        """Retrieve the governing spec text for the question's zone, then answer from it."""

        question = await self._table.get(question_id)
        if question is None:
            return None

        chunks = await self._retrieve(question)
        citations = [
            {"citation": c.citation(), "clause": c.clause, "source": c.source,
             "page": c.page, "zone": c.zone, "score": c.score, "text": c.text[:600]}
            for c in chunks
        ]
        grounded = bool(citations)

        answer, used_model, error = await self._compose(question, chunks, grounded)
        patch = {
            "llm_answer": answer,
            "llm_grounded": grounded,
            "llm_model": used_model,
            "llm_error": error,
            "answered_at": time.time(),
            "citations": citations,
        }
        updated = await self._table.patch(question_id, patch)
        log.info("question %s answered (grounded=%s model=%s error=%s)",
                 question_id, grounded, used_model, bool(error))
        return updated

    async def _retrieve(self, question: dict[str, Any]) -> list[Any]:
        """Zone-scoped retrieval. A clause from another zone must never come back."""

        if self.index is None or not getattr(self.index, "available", False):
            return []
        try:
            return await self.index.search(
                question.get("text") or "",
                project_id=self.project_id,
                zone=question.get("zone"),
                category=None,
                top_k=_TOP_K,
            )
        except Exception:  # noqa: BLE001 — no retrieval is a degraded answer, not a failure
            log.exception("retrieval failed for question %s", question.get("question_id"))
            return []

    async def _compose(
        self, question: dict[str, Any], chunks: list[Any], grounded: bool
    ) -> tuple[str, str | None, str | None]:
        """Ask the local model. Returns (answer, model, error)."""

        ungrounded_note = (
            "\n\nNote: no site specification text was found for this zone, so this answer is "
            "general guidance only and is NOT backed by the project documents. Confirm with your "
            "site manager before acting on it."
        )
        if not self.llm_enabled:
            return (
                "Automated answering is disabled on this site. Your site manager has been "
                "notified and will reply." , None, "llm_disabled",
            )

        context = "\n\n".join(
            f"[{i + 1}] {c.citation()}\n{c.text[:_MAX_TEXT]}" for i, c in enumerate(chunks)
        ) or "(no specification text is indexed for this zone)"

        prompt = (
            "You are a construction site safety assistant answering a question from a worker on "
            "site. Answer in plain, direct language a worker can act on — 3 sentences at most.\n"
            "Use ONLY the specification extracts below. Reference them by their [n] markers where "
            "relevant. Do NOT invent clause numbers, drawing numbers, measurements or dates. If "
            "the extracts do not answer the question, say so plainly and tell them to ask their "
            "site manager. If the question suggests immediate danger, tell them to stop work and "
            "raise an alert first.\n\n"
            f"Zone: {question.get('zone') or 'unspecified'}\n"
            f"Worker's question: {question.get('text')}\n\n"
            f"Specification extracts:\n{context}\n\n"
            "Answer:"
        )
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.2, "num_predict": 300}},
                )
                resp.raise_for_status()
                text = (resp.json().get("response") or "").strip()
        except Exception as exc:  # noqa: BLE001 — the manager path must still work
            log.warning("LLM unavailable for question %s: %s", question.get("question_id"), exc)
            return (
                "An automated answer could not be produced right now. Your site manager has been "
                "notified and will reply.",
                None,
                f"{type(exc).__name__}: {exc}",
            )

        if len(text) < 5:
            return (
                "An automated answer could not be produced right now. Your site manager has been "
                "notified and will reply.",
                self.model,
                "empty_response",
            )
        if not grounded:
            text += ungrounded_note
        elif chunks:
            text += "\n\nReferences:\n" + "\n".join(
                f"  [{i + 1}] {c.citation()}" for i, c in enumerate(chunks)
            )
        return text, self.model, None

    # -- manager side ----------------------------------------------------------

    async def reply(
        self, question_id: str, *, manager_id: str, reply: str
    ) -> dict[str, Any] | None:
        """The human answer. This is the authoritative one."""

        body = str(reply or "").strip()
        if not body:
            raise QuestionError("a reply needs some text")
        return await self._table.patch(question_id, {
            "manager_reply": body,
            "manager_id": str(manager_id),
            "replied_at": time.time(),
            "status": "answered",
        })

    async def close(self, question_id: str) -> dict[str, Any] | None:
        return await self._table.patch(question_id, {"status": "closed"})

    # -- reads -----------------------------------------------------------------

    async def get(self, question_id: str) -> dict[str, Any] | None:
        return await self._table.get(question_id)

    async def list(
        self,
        *,
        worker_id: str | None = None,
        status: str | None = None,
        zone: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {}
        if worker_id:
            where["worker_id"] = worker_id
        if status:
            where["status"] = status
        if zone:
            where["zone"] = zone
        return await self._table.list(where=where or None, limit=limit)

    async def stats(self) -> dict[str, Any]:
        pending = await self._table.count(where={"status": "pending"})
        answered = await self._table.count(where={"status": "answered"})
        closed = await self._table.count(where={"status": "closed"})
        # "awaiting a manager" is the queue a site manager actually works from: the LLM may have
        # replied already, but nobody accountable has.
        awaiting = [
            q for q in await self._table.list(where={"status": "pending"}, limit=1000)
            if not q.get("manager_reply")
        ]
        return {
            "total": pending + answered + closed,
            "pending": pending,
            "answered": answered,
            "closed": closed,
            "awaiting_manager": len(awaiting),
        }
