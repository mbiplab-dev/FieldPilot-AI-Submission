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
  LiveChip,
  Loading,
  Note,
  SectionTitle,
  inputClass,
  type Tone,
} from "@/components/ui";
import {
  api,
  errorMessage,
  fmtDateTime,
  timeAgo,
  type RFI,
  type RFICitation,
  type RFIStatus,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useLiveFeed } from "@/lib/useLiveFeed";

const STATUS_FILTERS: { value: RFIStatus | ""; label: string }[] = [
  { value: "", label: "All statuses" },
  { value: "pending_review", label: "Pending review" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
];

const STATUS_TONE: Record<RFIStatus, Tone> = {
  pending_review: "warn",
  approved: "good",
  rejected: "bad",
};

const REVIEWER = "site-manager";

export default function RfisPage() {
  const [status, setStatus] = useState<RFIStatus | "">("pending_review");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [rowError, setRowError] = useState<Record<string, string>>({});

  const live = useLiveFeed({ topics: ["rfi"], onFrame: () => void refresh() });

  const { data, error, loading, refresh } = usePoll(
    () => api.rfis({ status, limit: 100 }),
    live.connected ? 20000 : 6000,
    [status, live.connected],
  );

  const rfis = data?.rfis ?? [];
  const pending = rfis.filter((r) => r.status === "pending_review").length;

  const review = async (rfi: RFI, decision: "approve" | "reject") => {
    setBusy(rfi.rfi_id);
    setRowError((prev) => ({ ...prev, [rfi.rfi_id]: "" }));
    try {
      await api.reviewRfi(rfi.rfi_id, decision, {
        reviewer: REVIEWER,
        notes: notes[rfi.rfi_id]?.trim() || undefined,
      });
      await refresh();
    } catch (e) {
      setRowError((prev) => ({ ...prev, [rfi.rfi_id]: errorMessage(e) }));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="p-6">
      <PageHeader
        title="RFI review queue"
        subtitle="Spec-grounded requests for information · approve to send, reject to discard"
        action={<LiveChip connected={live.connected} />}
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label="Status" htmlFor="rfi-status" className="w-56">
          <select
            id="rfi-status"
            value={status}
            onChange={(e) => setStatus(e.target.value as RFIStatus | "")}
            className={inputClass}
          >
            {STATUS_FILTERS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </Field>
        <div className="flex items-center gap-2 pb-1.5 text-[13px] text-txt-2">
          <Badge tone={pending ? "warn" : "good"}>{pending} pending</Badge>
          <span className="text-txt-3">{rfis.length} shown</span>
        </div>
      </div>

      {error && !data ? (
        <ErrorState message={error} onRetry={() => void refresh()} />
      ) : loading && !data ? (
        <Card>
          <Loading label="Loading RFIs…" />
        </Card>
      ) : rfis.length === 0 ? (
        <Card>
          <Empty>
            {status === "pending_review"
              ? "Nothing waiting for review — the queue is clear."
              : "No RFIs match this filter."}
          </Empty>
        </Card>
      ) : (
        <>
          {error ? (
            <Note tone="warn" title="Showing cached queue">
              {error}
            </Note>
          ) : null}
          <div className="space-y-4">
            {rfis.map((rfi) => (
              <RfiCard
                key={rfi.rfi_id}
                rfi={rfi}
                open={Boolean(expanded[rfi.rfi_id])}
                onToggle={() =>
                  setExpanded((prev) => ({ ...prev, [rfi.rfi_id]: !prev[rfi.rfi_id] }))
                }
                notes={notes[rfi.rfi_id] ?? ""}
                onNotes={(v) => setNotes((prev) => ({ ...prev, [rfi.rfi_id]: v }))}
                busy={busy === rfi.rfi_id}
                error={rowError[rfi.rfi_id] || null}
                onReview={(d) => void review(rfi, d)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function RfiCard({
  rfi,
  open,
  onToggle,
  notes,
  onNotes,
  busy,
  error,
  onReview,
}: {
  rfi: RFI;
  open: boolean;
  onToggle: () => void;
  notes: string;
  onNotes: (v: string) => void;
  busy: boolean;
  error: string | null;
  onReview: (decision: "approve" | "reject") => void;
}) {
  const payload = rfi.payload ?? {};
  const citations = payload.citations ?? [];
  const ungrounded = payload.grounded === false;
  const bodyId = `rfi-body-${rfi.rfi_id}`;
  const notesId = `rfi-notes-${rfi.rfi_id}`;

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-line-soft px-4 py-3">
        <Badge tone={STATUS_TONE[rfi.status] ?? "neutral"}>{rfi.status.replace("_", " ")}</Badge>
        {rfi.priority ? <Badge tone="warn">{rfi.priority}</Badge> : null}
        <span className="font-mono text-[11px] text-txt-3">{rfi.zone ?? "no zone"}</span>
        {ungrounded ? (
          <Badge tone="bad" title="No spec text backed this RFI — verify before sending">
            ⚠ UNGROUNDED
          </Badge>
        ) : (
          <Badge tone="good">grounded · {citations.length || 1} citation(s)</Badge>
        )}
        {payload.llm_used === false ? <Badge tone="neutral">template (no LLM)</Badge> : null}
        <span className="ml-auto text-[11px] text-txt-3">{timeAgo(rfi.created_at)}</span>
      </div>

      <div className="px-4 py-3.5">
        <h3 className="text-[15px] font-semibold">{rfi.title ?? "Untitled RFI"}</h3>
        {rfi.summary ? <p className="mt-1 text-[13px] text-txt-2">{rfi.summary}</p> : null}

        {ungrounded ? (
          <p className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-600 dark:text-red-400">
            This RFI was generated without matching specification text. Nothing in the indexed
            blueprints supports it — read the body carefully before approving.
          </p>
        ) : null}

        <div className="mt-3">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            aria-controls={bodyId}
            className="text-[12px] font-semibold text-accent hover:underline"
          >
            {open ? "Hide full RFI" : "Show full RFI"}
          </button>
        </div>

        {open ? (
          <div id={bodyId} className="mt-3 space-y-4">
            <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-line-soft bg-panel-2 p-3 font-mono text-[12px] leading-relaxed text-txt">
              {rfi.body?.trim() || "No body text was generated for this RFI."}
            </pre>

            <div>
              <SectionTitle>Citations</SectionTitle>
              {rfi.citation ? (
                <p className="mb-2 font-mono text-[12px] text-txt-2">{rfi.citation}</p>
              ) : null}
              {citations.length ? (
                <ul className="space-y-2">
                  {citations.map((c, i) => (
                    <CitationRow key={`${rfi.rfi_id}-cite-${i}`} citation={c} />
                  ))}
                </ul>
              ) : (
                <Empty>No spec chunks were attached.</Empty>
              )}
            </div>
          </div>
        ) : null}
      </div>

      <div className="border-t border-line-soft px-4 py-3">
        {rfi.status === "pending_review" ? (
          <div className="flex flex-col gap-2.5 md:flex-row md:items-end">
            <Field label="Review notes (optional)" htmlFor={notesId} className="flex-1">
              <input
                id={notesId}
                value={notes}
                onChange={(e) => onNotes(e.target.value)}
                placeholder="e.g. confirmed against drawing A-203"
                className={inputClass}
              />
            </Field>
            <div className="flex gap-2">
              <Button tone="good" onClick={() => onReview("approve")} disabled={busy}>
                {busy ? "Saving…" : "Approve"}
              </Button>
              <Button tone="bad" onClick={() => onReview("reject")} disabled={busy}>
                Reject
              </Button>
            </div>
          </div>
        ) : (
          <p className="text-[12px] text-txt-3">
            {rfi.status === "approved" ? "Approved" : "Rejected"} by{" "}
            <span className="font-mono">{rfi.reviewer ?? "unknown"}</span> ·{" "}
            {fmtDateTime(rfi.reviewed_at)}
          </p>
        )}
        {error ? (
          <p className="mt-2 text-[12px] text-red-500" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function CitationRow({ citation }: { citation: RFICitation }) {
  return (
    <li className="rounded-lg border border-line-soft bg-panel-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">{citation.clause ?? citation.citation ?? "clause ?"}</Badge>
        <span className="truncate font-mono text-[11px] text-txt-2">
          {citation.source ?? "unknown source"}
          {citation.page !== null && citation.page !== undefined ? ` · p.${citation.page}` : ""}
        </span>
        {citation.zone ? (
          <span className="font-mono text-[11px] text-txt-3">{citation.zone}</span>
        ) : null}
        <span className="ml-auto font-mono text-[11px] text-txt-3">
          score {citation.score.toFixed(3)}
        </span>
      </div>
      {citation.text ? (
        <p className="mt-1.5 text-[12px] leading-relaxed text-txt-2">{citation.text}</p>
      ) : null}
    </li>
  );
}
