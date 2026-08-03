"""Blueprint/spec retrieval out of Qdrant, with hard zone isolation.

The safety-relevant property here is *negative*: a query scoped to zone-b must never return a
clause from zone-a, because an RFI citing the wrong zone's spec is worse than no RFI. That is
enforced with a Qdrant `Filter(must=[FieldCondition(match=MatchValue(...))])` on `project_id`,
`zone` and `category` — server-side, not by filtering results after the fact.

Chunks tagged `zone: null` are project-wide (general specifications) and are matched by a
`should`-less two-pass query: the zone-specific pass and the project-wide pass are both `must`
filtered, so a leak from another zone is impossible in either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.reasoning.embeddings import Embedder

log = get_logger("fieldpilot.reasoning.rag")

COLLECTION_DEFAULT = "blueprints"


@dataclass
class Chunk:
    """One retrievable span of a specification document."""

    chunk_id: str
    text: str
    project_id: str
    zone: str | None
    category: str
    source: str
    page: int | None = None
    clause: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id, "text": self.text, "project_id": self.project_id,
            "zone": self.zone, "category": self.category, "source": self.source,
            "page": self.page, "clause": self.clause, "score": self.score,
        }

    def citation(self) -> str:
        bits = [self.source]
        if self.clause:
            bits.append(f"clause {self.clause}")
        if self.page is not None:
            bits.append(f"p.{self.page}")
        return " — ".join(bits)


class BlueprintIndex:
    """Qdrant-backed store of specification chunks."""

    def __init__(
        self,
        embedder: Embedder,
        *,
        url: str = "http://localhost:6333",
        collection: str = COLLECTION_DEFAULT,
        top_k: int = 5,
    ) -> None:
        self.embedder = embedder
        self.url = url
        self.collection = collection
        self.top_k = top_k
        self._client: Any = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> bool:
        """Connect and ensure the collection exists. False if Qdrant is unreachable."""

        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.http import models as qm

            self._client = AsyncQdrantClient(url=self.url, timeout=15.0)
            probe = await self.embedder.probe()
            dim = int(probe["dim"])
            existing = await self._client.collection_exists(self.collection)
            if not existing:
                await self._client.create_collection(
                    collection_name=self.collection,
                    vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
                )
                log.info("created Qdrant collection %r (dim=%d)", self.collection, dim)
            # payload indexes make the must-filters cheap and exact
            for field_name in ("project_id", "zone", "category"):
                try:
                    await self._client.create_payload_index(
                        collection_name=self.collection,
                        field_name=field_name,
                        field_schema=qm.PayloadSchemaType.KEYWORD,
                    )
                except Exception:  # noqa: BLE001 — already indexed
                    pass
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Qdrant unavailable at %s (%s) — retrieval disabled", self.url, exc)
            self._client = None
            return False

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    async def count(self) -> int:
        if not self.available:
            return 0
        res = await self._client.count(self.collection, exact=True)
        return int(res.count)

    # -- writes ----------------------------------------------------------------

    async def upsert(self, chunks: list[Chunk]) -> int:
        if not self.available or not chunks:
            return 0
        from qdrant_client.http import models as qm

        vectors = await self.embedder.embed_many([c.text for c in chunks])
        points = [
            qm.PointStruct(
                id=_point_id(c.chunk_id),
                vector=vec,
                payload={
                    "chunk_id": c.chunk_id,
                    "text": c.text,
                    "project_id": c.project_id,
                    # Qdrant cannot match on a null keyword, so project-wide chunks get a sentinel
                    "zone": c.zone if c.zone else _ALL_ZONES,
                    "category": c.category,
                    "source": c.source,
                    "page": c.page,
                    "clause": c.clause,
                },
            )
            for c, vec in zip(chunks, vectors, strict=True)
        ]
        await self._client.upsert(collection_name=self.collection, points=points, wait=True)
        log.info("upserted %d chunks into %r", len(points), self.collection)
        return len(points)

    async def clear(self) -> None:
        """Drop and recreate the collection (used by re-ingest)."""

        if not self.available:
            return
        await self._client.delete_collection(self.collection)
        await self.start()

    # -- reads -----------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        project_id: str = "default",
        zone: str | None = None,
        category: str | None = None,
        top_k: int | None = None,
        include_project_wide: bool = True,
    ) -> list[Chunk]:
        """Retrieve chunks, hard-scoped to `project_id` (+ `zone`/`category` when given)."""

        if not self.available:
            return []
        k = top_k or self.top_k
        vector = await self.embedder.embed(query)

        results = await self._query(vector, project_id, zone, category, k)
        if zone and include_project_wide:
            results += await self._query(vector, project_id, _ALL_ZONES, category, k)

        seen: set[str] = set()
        merged: list[Chunk] = []
        for chunk in sorted(results, key=lambda c: c.score or 0.0, reverse=True):
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            merged.append(chunk)
        return merged[:k]

    async def _query(
        self, vector: list[float], project_id: str, zone: str | None,
        category: str | None, k: int,
    ) -> list[Chunk]:
        from qdrant_client.http import models as qm

        must = [qm.FieldCondition(key="project_id", match=qm.MatchValue(value=project_id))]
        if zone:
            must.append(qm.FieldCondition(key="zone", match=qm.MatchValue(value=zone)))
        if category:
            must.append(qm.FieldCondition(key="category", match=qm.MatchValue(value=category)))

        resp = await self._client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=qm.Filter(must=must),
            limit=k,
            with_payload=True,
        )
        out = []
        for point in resp.points:
            p = point.payload or {}
            stored_zone = p.get("zone")
            out.append(Chunk(
                chunk_id=str(p.get("chunk_id") or point.id),
                text=str(p.get("text") or ""),
                project_id=str(p.get("project_id") or ""),
                zone=None if stored_zone == _ALL_ZONES else stored_zone,
                category=str(p.get("category") or "general"),
                source=str(p.get("source") or "unknown"),
                page=p.get("page"),
                clause=p.get("clause"),
                score=float(point.score) if point.score is not None else None,
            ))
        return out


_ALL_ZONES = "__project_wide__"


def _point_id(chunk_id: str) -> str:
    """Qdrant needs a UUID or unsigned int id; derive a stable UUID from the chunk id."""

    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fieldpilot/chunk/{chunk_id}"))
