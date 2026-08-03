"""Retrieval-augmented reasoning: blueprint search and RFI drafting.

Retrieval is `must`-filtered on project/zone/category in Qdrant so a clause from one zone can
never be cited for a deviation in another — the cross-zone hallucination the PRD calls out.
"""

from fieldpilot.reasoning.embeddings import Embedder
from fieldpilot.reasoning.ingest import IngestReport, build_chunks, ingest_directory
from fieldpilot.reasoning.rag import BlueprintIndex, Chunk
from fieldpilot.reasoning.rfi import RFI_STATUSES, DraftedRFI, RFIDrafter

__all__ = [
    "RFI_STATUSES", "BlueprintIndex", "Chunk", "DraftedRFI", "Embedder",
    "IngestReport", "RFIDrafter", "build_chunks", "ingest_directory",
]
