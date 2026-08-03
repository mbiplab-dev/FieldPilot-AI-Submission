"""Ingest specification documents into the blueprint index.

Accepts PDF (via pypdf), Markdown and plain text from `data/blueprints/`. Zone, project and
category are taken from the filename convention

    <project>__<zone>__<category>__<title>.pdf        e.g. riverside__zone-a__structural__rebar.pdf

with any missing field defaulting sensibly and `zone=all` meaning project-wide. Chunking is
paragraph-aware with a character budget, and a clause number (`3.4.2`, `§7.1`, `Clause 12`) is
lifted out of each chunk when present so RFIs can cite it precisely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.reasoning.rag import BlueprintIndex, Chunk

log = get_logger("fieldpilot.reasoning.ingest")

SUPPORTED = (".pdf", ".md", ".txt")
MAX_CHARS = 900
MIN_CHARS = 60

_CLAUSE = re.compile(
    r"(?:clause\s+|§\s*)(\d+(?:\.\d+)*)|^\s*(\d+\.\d+(?:\.\d+)*)\s",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class IngestReport:
    files: int = 0
    chunks: int = 0
    upserted: int = 0
    skipped: list[str] = None  # type: ignore[assignment]
    degraded_embeddings: bool = False

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files, "chunks": self.chunks, "upserted": self.upserted,
            "skipped": self.skipped, "degraded_embeddings": self.degraded_embeddings,
        }


def parse_metadata(path: Path) -> dict[str, Any]:
    """Pull project/zone/category out of the `a__b__c__title` filename convention."""

    parts = path.stem.split("__")
    if len(parts) >= 3:
        project, zone, category = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        project, zone, category = parts[0], parts[1], "general"
    else:
        project, zone, category = "default", "all", "general"
    return {
        "project_id": project or "default",
        "zone": None if zone in ("all", "any", "project", "") else zone,
        "category": category or "general",
        "source": path.name,
    }


def read_document(path: Path) -> list[tuple[int | None, str]]:
    """Return [(page_number, text)]. Page is None for non-paginated formats."""

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [(i + 1, (page.extract_text() or "")) for i, page in enumerate(reader.pages)]
    return [(None, path.read_text(encoding="utf-8", errors="replace"))]


def chunk_text(text: str, *, max_chars: int = MAX_CHARS) -> list[str]:
    """Split on blank lines, then pack paragraphs up to `max_chars` without splitting them."""

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        para = re.sub(r"[ \t]+", " ", para)
        if len(para) > max_chars:                      # a single oversized paragraph
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
            continue
        if not buffer:
            buffer = para          # a paragraph at exactly the budget must not flush an empty chunk
        elif len(buffer) + len(para) + 2 <= max_chars:
            buffer = f"{buffer}\n\n{para}"
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)
    return [c for c in (c.strip() for c in chunks) if len(c) >= MIN_CHARS]


def extract_clause(text: str) -> str | None:
    m = _CLAUSE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def build_chunks(path: Path) -> list[Chunk]:
    meta = parse_metadata(path)
    out: list[Chunk] = []
    for page, page_text in read_document(path):
        for i, body in enumerate(chunk_text(page_text)):
            cid = f"{path.name}:{page or 0}:{i}"
            out.append(Chunk(
                chunk_id=cid,
                text=body,
                project_id=meta["project_id"],
                zone=meta["zone"],
                category=meta["category"],
                source=meta["source"],
                page=page,
                clause=extract_clause(body),
            ))
    return out


async def ingest_directory(
    index: BlueprintIndex,
    directory: str | Path = "data/blueprints",
    *,
    replace: bool = False,
) -> IngestReport:
    """Ingest every supported document in `directory` into `index`."""

    report = IngestReport()
    root = Path(directory)
    if not root.is_dir():
        report.skipped.append(f"{root} is not a directory")
        return report
    if not index.available:
        report.skipped.append("Qdrant unavailable — nothing ingested")
        return report
    if replace:
        await index.clear()

    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED:
            report.skipped.append(f"{path.name}: unsupported type")
            continue
        try:
            chunks = build_chunks(path)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the corpus
            report.skipped.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        if not chunks:
            report.skipped.append(f"{path.name}: no extractable text")
            continue
        report.files += 1
        report.chunks += len(chunks)
        report.upserted += await index.upsert(chunks)

    report.degraded_embeddings = index.embedder.degraded
    log.info("ingest complete: %s", report.to_dict())
    return report
