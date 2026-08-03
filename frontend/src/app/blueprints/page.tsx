"use client";

import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorState,
  Field,
  Loading,
  Note,
  SectionTitle,
  StatTile,
  Td,
  Th,
  inputClass,
} from "@/components/ui";
import {
  api,
  errorMessage,
  fmtBytes,
  type BlueprintChunk,
  type BlueprintIngestResult,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

export default function BlueprintsPage() {
  const { data, error, loading, refresh } = usePoll(() => api.blueprints(), 30000);

  const [replace, setReplace] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [ingestResult, setIngestResult] = useState<BlueprintIngestResult | null>(null);
  const [ingestError, setIngestError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [zone, setZone] = useState("");
  const [topK, setTopK] = useState("5");
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [chunks, setChunks] = useState<BlueprintChunk[]>([]);
  const [searchError, setSearchError] = useState<string | null>(null);

  const documents = data?.documents ?? [];
  const embeddings = data?.embeddings ?? null;

  const ingest = async () => {
    setIngesting(true);
    setIngestError(null);
    setIngestResult(null);
    try {
      const result = await api.ingestBlueprints(replace);
      setIngestResult(result);
      await refresh();
    } catch (e) {
      setIngestError(errorMessage(e));
    } finally {
      setIngesting(false);
    }
  };

  const search = async () => {
    const q = query.trim();
    if (!q) {
      setSearchError("Enter something to search for.");
      return;
    }
    setSearching(true);
    setSearchError(null);
    try {
      const parsed = Number.parseInt(topK, 10);
      const result = await api.searchBlueprints({
        query: q,
        zone: zone.trim() || undefined,
        top_k: Number.isFinite(parsed) && parsed > 0 ? parsed : undefined,
      });
      setChunks(result.chunks);
      setSearched(true);
    } catch (e) {
      setSearchError(errorMessage(e));
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Blueprints & specs"
        subtitle="The retrieval index that grounds RFIs in real specification text"
        action={
          data ? (
            <Badge tone={data.available ? "good" : "bad"}>
              {data.available ? "index available" : "index unavailable"}
            </Badge>
          ) : null
        }
      />

      {error && !data ? (
        <ErrorState message={error} onRetry={() => void refresh()} />
      ) : loading && !data ? (
        <Card>
          <Loading label="Loading index…" />
        </Card>
      ) : (
        <>
          {error ? (
            <Note tone="warn" title="Showing cached index">
              {error}
            </Note>
          ) : null}

          {embeddings && !embeddings.semantic ? (
            <Note tone="warn" title="Lexical fallback active">
              Embeddings backend <span className="font-mono">{embeddings.backend}</span> is not
              producing semantic vectors, so search is keyword-ish: results match wording rather
              than meaning. Start the embedding service for true semantic retrieval.
            </Note>
          ) : null}

          {data && !data.available ? (
            <Note tone="bad" title="Retrieval is offline">
              The vector store is not reachable, so RFIs will be generated ungrounded. Bring up the
              infrastructure with <span className="font-mono">make infra-up</span>.
            </Note>
          ) : null}

          <div className="grid grid-cols-2 gap-3.5 md:grid-cols-4">
            <StatTile value={documents.length} label="Documents on disk" />
            <StatTile value={data?.indexed_chunks ?? "–"} label="Indexed chunks" />
            <StatTile
              value={embeddings?.semantic ? "semantic" : "lexical"}
              label="Retrieval mode"
              accent={embeddings?.semantic ? "#10b981" : "#f59e0b"}
            />
            <StatTile
              value={<span className="font-mono text-base">{embeddings?.backend ?? "–"}</span>}
              label={embeddings?.model ? `model ${embeddings.model}` : "embeddings backend"}
            />
          </div>

          <SectionTitle>Ingest</SectionTitle>
          <Card className="px-4 py-4">
            <div className="flex flex-wrap items-center gap-5">
              <label className="flex items-center gap-2 text-[13px]" htmlFor="bp-replace">
                <input
                  id="bp-replace"
                  type="checkbox"
                  checked={replace}
                  onChange={(e) => setReplace(e.target.checked)}
                  className="h-4 w-4 accent-[var(--accent)]"
                />
                Replace the existing index (otherwise new chunks are upserted)
              </label>
              <Button onClick={() => void ingest()} disabled={ingesting}>
                {ingesting ? "Ingesting…" : "Ingest documents"}
              </Button>
            </div>

            {ingestError ? (
              <p className="mt-2.5 text-[12px] text-red-500" role="alert">
                {ingestError}
              </p>
            ) : null}

            {ingestResult ? (
              <div className="mt-3 rounded-lg border border-line-soft bg-panel-2 p-3 text-[12.5px]">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="good">{ingestResult.files} file(s)</Badge>
                  <Badge tone="info">{ingestResult.chunks} chunk(s)</Badge>
                  <Badge tone="accent">{ingestResult.upserted} upserted</Badge>
                  {ingestResult.degraded_embeddings ? (
                    <Badge tone="warn">degraded embeddings</Badge>
                  ) : null}
                </div>
                {ingestResult.skipped.length ? (
                  <p className="mt-2 text-txt-2">
                    Skipped: <span className="font-mono">{ingestResult.skipped.join(", ")}</span>
                  </p>
                ) : null}
              </div>
            ) : null}
          </Card>

          <SectionTitle>Search the specs</SectionTitle>
          <Card className="px-4 py-4">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void search();
              }}
            >
              <div className="grid grid-cols-1 gap-3.5 md:grid-cols-[2fr_1fr_auto] md:items-end">
                <Field label="Query" htmlFor="bp-query">
                  <input
                    id="bp-query"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="e.g. guardrail height requirement for scaffolding"
                    className={inputClass}
                  />
                </Field>
                <Field label="Zone filter" htmlFor="bp-zone" hint="Optional">
                  <input
                    id="bp-zone"
                    value={zone}
                    onChange={(e) => setZone(e.target.value)}
                    placeholder="all zones"
                    className={inputClass}
                  />
                </Field>
                <div className="flex items-end gap-2.5">
                  <Field label="Top k" htmlFor="bp-topk" className="w-24">
                    <input
                      id="bp-topk"
                      type="number"
                      min={1}
                      max={50}
                      inputMode="numeric"
                      value={topK}
                      onChange={(e) => setTopK(e.target.value)}
                      className={inputClass}
                    />
                  </Field>
                  <Button type="submit" disabled={searching}>
                    {searching ? "Searching…" : "Search"}
                  </Button>
                </div>
              </div>
            </form>

            {searchError ? (
              <p className="mt-2.5 text-[12px] text-red-500" role="alert">
                {searchError}
              </p>
            ) : null}

            <div className="mt-4">
              {chunks.length ? (
                <ul className="space-y-2.5">
                  {chunks.map((chunk) => (
                    <ChunkRow key={chunk.chunk_id} chunk={chunk} />
                  ))}
                </ul>
              ) : searched && !searching ? (
                <Empty>No spec text matched that query.</Empty>
              ) : (
                <Empty>Search to see the exact clauses an RFI would cite.</Empty>
              )}
            </div>
          </Card>

          <SectionTitle>Documents ({documents.length})</SectionTitle>
          <Card className="overflow-x-auto">
            <table className="w-full min-w-[640px]">
              <thead>
                <tr className="border-b border-line text-left">
                  <Th>Document</Th>
                  <Th>Project</Th>
                  <Th>Zone</Th>
                  <Th>Category</Th>
                  <Th>Size</Th>
                </tr>
              </thead>
              <tbody>
                {documents.length ? (
                  documents.map((doc) => (
                    <tr key={doc.name} className="border-b border-line-soft last:border-0">
                      <Td>
                        <span className="font-mono text-[12px]">{doc.name}</span>
                      </Td>
                      <Td>
                        <span className="font-mono text-[11px] text-txt-2">
                          {doc.project_id ?? "–"}
                        </span>
                      </Td>
                      <Td>
                        <span className="font-mono text-[11px] text-txt-2">{doc.zone ?? "–"}</span>
                      </Td>
                      <Td>{doc.category ? <Badge tone="info">{doc.category}</Badge> : "–"}</Td>
                      <Td>
                        <span className="font-mono text-[11px] text-txt-3">
                          {fmtBytes(doc.size_bytes)}
                        </span>
                      </Td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <Td colSpan={5}>
                      <Empty>
                        No documents found — drop specs into the blueprints folder, then ingest.
                      </Empty>
                    </Td>
                  </tr>
                )}
              </tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  );
}

function ChunkRow({ chunk }: { chunk: BlueprintChunk }) {
  return (
    <li className="rounded-lg border border-line-soft bg-panel-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        {chunk.clause ? <Badge tone="accent">{chunk.clause}</Badge> : null}
        <span className="truncate font-mono text-[11px] text-txt-2">
          {chunk.source ?? "unknown source"}
          {chunk.page !== null && chunk.page !== undefined ? ` · p.${chunk.page}` : ""}
        </span>
        {chunk.zone ? <Badge tone="neutral">{chunk.zone}</Badge> : null}
        {chunk.category ? <Badge tone="info">{chunk.category}</Badge> : null}
        <span className="ml-auto font-mono text-[11px] text-txt-3">
          score {chunk.score.toFixed(3)}
        </span>
      </div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-txt-2">{chunk.text}</p>
    </li>
  );
}
