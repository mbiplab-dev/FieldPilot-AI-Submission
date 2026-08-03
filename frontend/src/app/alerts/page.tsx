"use client";

import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorState,
  LiveChip,
  Loading,
  Note,
  SeverityChip,
  StateChip,
} from "@/components/ui";
import {
  api,
  errorMessage,
  fmtTime,
  timeAgo,
  type Alert,
  type AlertState,
  type Feedback,
  type FeedbackDecision,
  type Severity,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useLiveFeed } from "@/lib/useLiveFeed";

const STATES: AlertState[] = ["NEW", "ACTIVE", "RESOLVED", "SUPPRESSED"];
const SEVERITIES: Severity[] = ["critical", "high", "medium", "low"];
const TYPES = ["ppe", "fall", "crack", "inspection", "measurement", "fire", "gas", "rfi", "proximity"];

/** Who the dashboard signs feedback as — this is the single-seat site-manager console. */
const REVIEWER = "site-manager";

/** Normalised view of "has this alert been reviewed?", server- or client-side. */
interface Decision {
  decision: FeedbackDecision;
  state: "pending" | "saved" | "failed";
  reviewer?: string | null;
  message?: string;
}

export default function AlertsPage() {
  const [fState, setFState] = useState("");
  const [fSev, setFSev] = useState("");
  const [fType, setFType] = useState("");
  const [fWorker, setFWorker] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [local, setLocal] = useState<Record<string, Decision>>({});

  const live = useLiveFeed({ topics: ["alert", "alert_resolved"], onFrame: () => void refresh() });

  const { data, error, loading, refresh } = usePoll(
    async () => {
      const { alerts } = await api.alerts({
        state: fState,
        severity: fSev,
        event_type: fType,
        worker_id: fWorker,
        limit: "300",
      });
      // Feedback is a newer endpoint — a 404 must not take the alert list down.
      let feedback: Feedback[] = [];
      let feedbackError: string | null = null;
      try {
        feedback = (await api.feedback({ limit: 500 })).feedback;
      } catch (e) {
        feedbackError = errorMessage(e);
      }
      return { alerts, feedback, feedbackError };
    },
    live.connected ? 20000 : 4000,
    [fState, fSev, fType, fWorker, live.connected],
  );

  const alerts = data?.alerts ?? [];

  const reviewed = useMemo(() => {
    const map = new Map<string, Decision>();
    for (const f of data?.feedback ?? []) {
      const prev = map.get(f.alert_id);
      if (prev && prev.state === "saved") continue;
      map.set(f.alert_id, { decision: f.decision, state: "saved", reviewer: f.reviewer });
    }
    return map;
  }, [data?.feedback]);

  const decisionFor = (alertId: string): Decision | undefined => local[alertId] ?? reviewed.get(alertId);

  const decide = async (alert: Alert, decision: FeedbackDecision) => {
    setLocal((prev) => ({ ...prev, [alert.alert_id]: { decision, state: "pending" } }));
    try {
      const saved = await api.submitFeedback(alert.alert_id, {
        decision,
        reviewer: REVIEWER,
        label: decision === "approve" ? alert.event_type : undefined,
      });
      setLocal((prev) => ({
        ...prev,
        [alert.alert_id]: { decision: saved.decision, state: "saved", reviewer: saved.reviewer },
      }));
      void refresh();
    } catch (e) {
      // Reconcile: drop the optimistic decision and surface why it failed.
      setLocal((prev) => ({
        ...prev,
        [alert.alert_id]: { decision, state: "failed", message: errorMessage(e) },
      }));
    }
  };

  const selected = selectedId ? (alerts.find((a) => a.alert_id === selectedId) ?? null) : null;
  const hasFilters = Boolean(fState || fSev || fType || fWorker);

  return (
    <div className="p-6">
      <PageHeader
        title="Alerts"
        subtitle="Each trigger hops through a local LLM for a final verdict · approve or reject to train the detector"
        action={<LiveChip connected={live.connected} />}
      />

      <div className="mb-4 flex flex-wrap gap-2.5">
        <FilterSelect value={fState} onChange={setFState} label="State" options={STATES} />
        <FilterSelect value={fSev} onChange={setFSev} label="Severity" options={SEVERITIES} />
        <FilterSelect value={fType} onChange={setFType} label="Type" options={TYPES} />
        <label className="sr-only" htmlFor="alerts-worker">
          Worker id
        </label>
        <input
          id="alerts-worker"
          value={fWorker}
          onChange={(e) => setFWorker(e.target.value)}
          placeholder="Worker id…"
          className="rounded-lg border border-line bg-panel px-3 py-1.5 text-sm focus:border-accent focus:outline-none"
        />
        {hasFilters && (
          <Button
            tone="secondary"
            onClick={() => {
              setFState("");
              setFSev("");
              setFType("");
              setFWorker("");
            }}
          >
            Reset
          </Button>
        )}
      </div>

      {data?.feedbackError ? (
        <Note tone="warn" title="Review history unavailable">
          {data.feedbackError} — approve/reject still works, but earlier decisions are not shown.
        </Note>
      ) : null}

      {error && !data ? (
        <ErrorState message={error} onRetry={() => void refresh()} />
      ) : loading && !data ? (
        <Card>
          <Loading label="Loading alerts…" />
        </Card>
      ) : alerts.length ? (
        <>
          {error ? <Note tone="warn" title="Showing cached alerts">{error}</Note> : null}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {alerts.map((a) => (
              <AlertCard
                key={a.alert_id}
                alert={a}
                decision={decisionFor(a.alert_id)}
                onOpen={() => setSelectedId(a.alert_id)}
                onDecide={(d) => void decide(a, d)}
              />
            ))}
          </div>
        </>
      ) : (
        <Card>
          <Empty>
            {hasFilters ? "No alerts match the filters." : "No alerts yet — the site is quiet."}
          </Empty>
        </Card>
      )}

      {selected && (
        <AlertDrawer
          alert={selected}
          decision={decisionFor(selected.alert_id)}
          onDecide={(d) => void decide(selected, d)}
          onClose={() => setSelectedId(null)}
          onActed={() => void refresh()}
        />
      )}
    </div>
  );
}

function FilterSelect({
  value,
  onChange,
  label,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  options: string[];
}) {
  const id = `alerts-filter-${label.toLowerCase()}`;
  return (
    <>
      <label className="sr-only" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="min-w-[120px] rounded-lg border border-line bg-panel px-3 py-1.5 text-sm focus:border-accent focus:outline-none"
      >
        <option value="">{label}: all</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </>
  );
}

interface Verdict {
  confirmed: boolean;
  confidence: number;
  reasoning: string;
  llm_used: boolean;
  model?: string | null;
  severity?: string | null;
}

function getVerdict(a: Alert): Verdict | null {
  const v = (a.payload as Record<string, unknown>).llm_verdict;
  return (v ?? null) as Verdict | null;
}

function VerdictBadge({ alert }: { alert: Alert }) {
  const v = getVerdict(alert);
  if (!v) return null;
  if (!v.llm_used) return <Badge tone="neutral">LLM auto (unavailable)</Badge>;
  if (v.confirmed) return <Badge tone="good">LLM confirmed {Math.round(v.confidence * 100)}%</Badge>;
  return <Badge tone="bad">LLM rejected</Badge>;
}

/** Approve / reject controls — the supervisor half of the learning loop. */
function FeedbackControls({
  alert,
  decision,
  onDecide,
}: {
  alert: Alert;
  decision?: Decision;
  onDecide: (d: FeedbackDecision) => void;
}) {
  if (decision && decision.state !== "failed") {
    const pending = decision.state === "pending";
    return (
      <div className="flex items-center gap-2">
        <Badge tone={decision.decision === "approve" ? "good" : "bad"}>
          {decision.decision === "approve" ? "approved" : "rejected"}
          {pending ? " · saving…" : ""}
        </Badge>
        {!pending && decision.reviewer ? (
          <span className="truncate text-[11px] text-txt-3">by {decision.reviewer}</span>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          tone="good"
          onClick={() => onDecide("approve")}
          ariaLabel={`Approve ${alert.event_type} alert as a true detection`}
          title="Correct detection — feeds the fine-tune set as a positive"
        >
          Approve
        </Button>
        <Button
          size="sm"
          tone="bad"
          onClick={() => onDecide("reject")}
          ariaLabel={`Reject ${alert.event_type} alert as a false positive`}
          title="False positive — feeds the fine-tune set as a negative"
        >
          Reject
        </Button>
      </div>
      {decision?.state === "failed" ? (
        <p className="text-[11px] text-red-500" role="alert">
          {decision.message ?? "Could not save"} — try again.
        </p>
      ) : null}
    </div>
  );
}

function AlertCard({
  alert,
  decision,
  onOpen,
  onDecide,
}: {
  alert: Alert;
  decision?: Decision;
  onOpen: () => void;
  onDecide: (d: FeedbackDecision) => void;
}) {
  return (
    <div className="flex flex-col overflow-hidden rounded-xl border border-line bg-panel text-left shadow-sm transition-all hover:border-txt-3 hover:shadow-md">
      <button
        type="button"
        onClick={onOpen}
        aria-label={`Open details for ${alert.event_type} alert in ${alert.zone ?? "unknown zone"}`}
        className="block w-full text-left"
      >
        {/* captured snapshot with bbox markers */}
        <div className="relative aspect-video w-full overflow-hidden bg-black">
          {alert.image_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={alert.image_url} alt={alert.event_type} className="h-full w-full object-cover" />
          ) : (
            <div className="grid h-full place-items-center text-xs text-zinc-500">no snapshot</div>
          )}
          <div className="absolute left-2 top-2 flex gap-1.5">
            <SeverityChip severity={alert.severity} />
          </div>
          <div className="absolute right-2 top-2">
            <StateChip state={alert.state} />
          </div>
        </div>

        <div className="flex flex-col gap-2 p-3.5">
          <div className="flex items-center gap-2">
            <Badge tone="info">{alert.event_type}</Badge>
            <span className="font-mono text-[11px] text-txt-3">{alert.zone ?? "–"}</span>
            <span className="ml-auto font-mono text-[11px] text-txt-3">×{alert.hit_count}</span>
          </div>
          <p className="line-clamp-2 text-sm text-txt">{alert.message || alert.event_type}</p>
          <div className="flex items-center justify-between gap-2">
            <VerdictBadge alert={alert} />
            <span className="ml-auto text-[11px] text-txt-3">{timeAgo(alert.last_seen)}</span>
          </div>
        </div>
      </button>

      <div className="mt-auto flex items-center justify-between gap-2 border-t border-line-soft px-3.5 py-2.5">
        <span className="text-[11px] uppercase tracking-wider text-txt-3">Supervisor</span>
        <FeedbackControls alert={alert} decision={decision} onDecide={onDecide} />
      </div>
    </div>
  );
}

function AlertDrawer({
  alert,
  decision,
  onDecide,
  onClose,
  onActed,
}: {
  alert: Alert;
  decision?: Decision;
  onDecide: (d: FeedbackDecision) => void;
  onClose: () => void;
  onActed: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const act = async (op: "resolve" | "suppress" | "unsuppress") => {
    setBusy(true);
    try {
      await api.alertAction(alert.alert_id, op);
      setToast(`Alert ${op}d`);
      onActed();
      setTimeout(onClose, 700);
    } catch (e) {
      setToast(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const v = getVerdict(alert);

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button
        type="button"
        aria-label="Close alert details"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-black/50"
      />
      <div
        role="dialog"
        aria-label={`Alert details · ${alert.event_type}`}
        className="relative w-full max-w-md overflow-y-auto border-l border-line bg-panel shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-4">
          <h3 className="text-base font-semibold">Alert · {alert.event_type}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="grid h-7 w-7 place-items-center rounded-lg border border-line bg-panel-2 text-txt-2 hover:text-txt"
          >
            ✕
          </button>
        </div>

        {/* the captured annotated snapshot */}
        {alert.image_url && (
          <div className="border-b border-line bg-black px-5 py-4">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={alert.image_url} alt="alert snapshot" className="w-full rounded-lg" />
            <p className="mt-2 text-center text-[11px] text-zinc-500">
              captured snapshot · bbox + markers
            </p>
          </div>
        )}

        <div className="px-5 py-4">
          <div className="mb-4 flex flex-wrap gap-2">
            <SeverityChip severity={alert.severity} />
            <StateChip state={alert.state} />
            <VerdictBadge alert={alert} />
          </div>

          {v && (
            <div className="mb-4 rounded-lg border border-line-soft bg-panel-2 p-3">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-txt-3">
                LLM verdict
              </div>
              <p className="text-sm">{v.reasoning}</p>
              {v.model && (
                <p className="mt-1.5 font-mono text-[11px] text-txt-3">
                  model: {v.model} · used: {v.llm_used ? "yes" : "no (fallback)"}
                </p>
              )}
            </div>
          )}

          <div className="mb-4 rounded-lg border border-line-soft bg-panel-2 p-3">
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-txt-3">
              Supervisor review
            </div>
            <FeedbackControls alert={alert} decision={decision} onDecide={onDecide} />
            <p className="mt-2 text-[11px] text-txt-3">
              Approvals and rejections become labelled samples for the next fine-tune run.
            </p>
          </div>

          <dl className="space-y-2 text-sm">
            <Kv k="Worker" v={alert.worker_id ?? "–"} mono />
            <Kv k="Zone" v={alert.zone ?? "–"} mono />
            <Kv k="Camera" v={alert.camera_id ?? "–"} mono />
            <Kv k="Hits" v={`${alert.hit_count} (deduplicated)`} mono />
            <Kv k="Confidence" v={alert.confidence.toFixed(2)} mono />
            <Kv k="Message" v={alert.message ?? "–"} />
            <Kv k="First seen" v={fmtTime(alert.first_seen)} mono />
            <Kv k="Last seen" v={fmtTime(alert.last_seen)} mono />
            {alert.resolved_at && <Kv k="Resolved" v={fmtTime(alert.resolved_at)} mono />}
          </dl>
        </div>

        <div className="flex gap-2.5 border-t border-line px-5 py-4">
          {alert.state === "NEW" || alert.state === "ACTIVE" ? (
            <>
              <Button onClick={() => void act("resolve")} disabled={busy}>
                Resolve
              </Button>
              <Button tone="secondary" onClick={() => void act("suppress")} disabled={busy}>
                Suppress
              </Button>
            </>
          ) : alert.state === "SUPPRESSED" ? (
            <Button tone="secondary" onClick={() => void act("unsuppress")} disabled={busy}>
              Unsuppress
            </Button>
          ) : null}
        </div>

        {toast && (
          <div
            role="status"
            className="absolute bottom-4 right-4 rounded-lg border border-line bg-panel px-4 py-2.5 text-sm shadow-lg"
          >
            {toast}
          </div>
        )}
      </div>
    </div>
  );
}

function Kv({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[110px_1fr] gap-2">
      <dt className="text-txt-3">{k}</dt>
      <dd className={`break-words ${mono ? "font-mono" : ""}`}>{v}</dd>
    </div>
  );
}
