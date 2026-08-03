"use client";

import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Card,
  Delta,
  Empty,
  ErrorState,
  LiveChip,
  Loading,
  Note,
  SectionTitle,
  SeverityChip,
  StatTile,
  StateChip,
} from "@/components/ui";
import {
  api,
  fmtDelta,
  fmtMetric,
  fmtPercent,
  timeAgo,
  type Alert,
  type Inspection,
  type RFIStatus,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useLiveFeed } from "@/lib/useLiveFeed";

const RFI_TONE: Record<RFIStatus, "warn" | "good" | "bad"> = {
  pending_review: "warn",
  approved: "good",
  rejected: "bad",
};

/** Optional endpoints: a miss degrades one tile instead of blanking the page. */
async function soft<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch {
    return null;
  }
}

export default function OverviewPage() {
  const live = useLiveFeed({ onFrame: () => void refresh() });

  const { data, error, refresh } = usePoll(
    async () => {
      // Alerts + workers are the backbone of this page — let them fail loudly.
      const [alerts, workers] = await Promise.all([api.alerts({ limit: "500" }), api.workers()]);
      const [notes, rfis, insp, evStats, health, learning, feedback] = await Promise.all([
        soft(api.notifications()),
        soft(api.rfis({ limit: 20 })),
        soft(api.inspections()),
        soft(api.eventStats()),
        soft(api.health()),
        soft(api.latestLearningRun()),
        soft(api.feedbackStats()),
      ]);
      return { alerts, workers, notes, rfis, insp, evStats, health, learning, feedback };
    },
    live.connected ? 20000 : 5000,
    [live.connected],
  );

  if (error && !data) {
    return (
      <div className="p-6">
        <PageHeader title="Site overview" subtitle="Live safety posture across all zones" />
        <ErrorState message={error} onRetry={() => void refresh()} />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="p-6">
        <PageHeader title="Site overview" subtitle="Live safety posture across all zones" />
        <Card>
          <Loading label="Loading site state…" />
        </Card>
      </div>
    );
  }

  const workers = data.workers.workers;
  const act = data.alerts.alerts.filter((a) => a.state === "NEW" || a.state === "ACTIVE");
  const crit = act.filter((a) => a.severity === "critical").length;
  const high = act.filter((a) => a.severity === "high").length;
  const med = act.filter((a) => a.severity === "medium").length;
  const score = workers.length
    ? Math.round(
        Math.max(
          0,
          workers.reduce((s, w) => s + (100 - 15 * w.active_alerts), 0) / workers.length,
        ),
      )
    : 100;
  const evTotal = Object.values(data.evStats?.counts_by_type ?? {}).reduce((a, b) => a + b, 0);

  const rfis = data.rfis?.rfis ?? [];
  const pendingRfis =
    data.health?.rfis_pending ?? rfis.filter((r) => r.status === "pending_review").length;
  const broadcast = data.health?.broadcast ?? null;
  const rag = data.health?.rag ?? null;
  const learning = data.learning;
  const feedback = data.feedback;

  return (
    <div className="p-6">
      <PageHeader
        title="Site overview"
        subtitle="Live safety posture across all zones"
        action={<LiveChip connected={live.connected} />}
      />

      {!live.connected ? (
        <Note tone="warn" title="Live push unavailable">
          {live.error ?? "Websocket not connected"} — this page is refreshing on a timer instead
          {live.attempts > 0 ? ` (retry ${live.attempts})` : ""}.
        </Note>
      ) : null}

      {error ? (
        <Note tone="warn" title="Showing the last good snapshot">
          {error}
        </Note>
      ) : null}

      <div className="grid grid-cols-2 gap-3.5 md:grid-cols-4">
        <StatTile value={workers.length} label="Workers on site" />
        <StatTile value={act.length} label="Active alerts" accent={crit ? "#ef4444" : "#10b981"} />
        <StatTile value={crit} label="Critical" accent="#ef4444" />
        <StatTile value={high} label="High" accent="#f97316" />
        <StatTile value={med} label="Medium" accent="#f59e0b" />
        <StatTile
          value={score}
          label="Site safety score"
          accent={score >= 90 ? "#10b981" : score >= 70 ? "#f59e0b" : "#ef4444"}
        />
        <StatTile value={evTotal} label="Events logged" />
        <StatTile value={pendingRfis} label="RFIs pending review" accent={pendingRfis ? "#f59e0b" : "#10b981"} />
      </div>

      <SectionTitle>Learning loop &amp; platform</SectionTitle>
      <div className="grid grid-cols-2 gap-3.5 md:grid-cols-4">
        <StatTile
          value={
            learning ? <Delta value={learning.delta} text={fmtDelta(learning.delta)} /> : "–"
          }
          label={
            learning
              ? `mAP50 ${fmtMetric(learning.map50_before)} → ${fmtMetric(learning.map50_after)}`
              : "No training run yet"
          }
          accent={
            learning?.delta && learning.delta > 0
              ? "#10b981"
              : learning?.delta && learning.delta < 0
                ? "#ef4444"
                : undefined
          }
        />
        <StatTile
          value={feedback ? fmtPercent(feedback.approval_rate) : "–"}
          label={
            feedback
              ? `Approval rate · ${feedback.total} reviewed, ${feedback.unconsumed} untrained`
              : "Feedback stats unavailable"
          }
          accent="#2f6fdd"
        />
        <StatTile
          value={broadcast ? broadcast.connected : "–"}
          label={
            broadcast
              ? `Live clients · ${broadcast.devices} device(s), ${broadcast.dashboards} dashboard(s)`
              : "Broadcast stats unavailable"
          }
          accent={broadcast && broadcast.connected > 0 ? "#10b981" : "#f59e0b"}
        />
        <StatTile
          value={rag ? rag.indexed_chunks : "–"}
          label={
            rag
              ? `Spec chunks · ${rag.available ? "index up" : "index down"}, ${
                  rag.embeddings.semantic ? "semantic" : "lexical"
                }`
              : "RAG status unavailable"
          }
          accent={rag?.available ? (rag.embeddings.semantic ? "#10b981" : "#f59e0b") : "#ef4444"}
        />
      </div>

      {learning && (learning.status === "blocked" || learning.status === "failed") ? (
        <div className="mt-4">
          <Note tone={learning.status === "failed" ? "bad" : "warn"} title={`Last training run ${learning.status}`}>
            {learning.message || "No detail provided."}{" "}
            <Link href="/learning" className="font-semibold underline">
              Open the learning page
            </Link>
          </Note>
        </div>
      ) : null}

      <div className="mt-7 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <SectionTitle>Active alerts</SectionTitle>
          <Card>
            {act.length ? (
              act.slice(0, 8).map((a: Alert) => <AlertRow key={a.alert_id} alert={a} />)
            ) : (
              <Empty>No active alerts — site is clear.</Empty>
            )}
          </Card>
        </div>
        <div>
          <SectionTitle>Latest notifications</SectionTitle>
          <Card>
            {data.notes === null ? (
              <Empty>Notification feed unavailable.</Empty>
            ) : data.notes.notifications.length ? (
              data.notes.notifications.slice(0, 8).map((n) => (
                <div
                  key={n.notification_id}
                  className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0"
                >
                  <Badge tone="info">{n.channel}</Badge>
                  <span className="flex-1 truncate text-txt">{n.subject}</span>
                  <span className="shrink-0 text-[11px] text-txt-3">{timeAgo(n.created_at)}</span>
                </div>
              ))
            ) : (
              <Empty>No notifications yet.</Empty>
            )}
          </Card>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <SectionTitle>RFI review queue</SectionTitle>
          <Card>
            {data.rfis === null ? (
              <Empty>RFI feed unavailable.</Empty>
            ) : rfis.length ? (
              rfis.slice(0, 8).map((r) => (
                <Link
                  key={r.rfi_id}
                  href="/rfis"
                  className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm transition-colors last:border-0 hover:bg-panel-2"
                >
                  <Badge tone={RFI_TONE[r.status] ?? "neutral"}>
                    {(r.status ?? "").replace("_", " ") || "–"}
                  </Badge>
                  {r.payload?.grounded === false ? <Badge tone="bad">ungrounded</Badge> : null}
                  <span className="flex-1 truncate">{r.title}</span>
                  <span className="shrink-0 font-mono text-[11px] text-txt-3">{r.zone ?? "–"}</span>
                </Link>
              ))
            ) : (
              <Empty>No RFIs generated yet.</Empty>
            )}
          </Card>
        </div>
        <div>
          <SectionTitle>Active inspections</SectionTitle>
          <Card>
            {data.insp === null ? (
              <Empty>Inspection feed unavailable.</Empty>
            ) : data.insp.inspections.length ? (
              data.insp.inspections.map((i: Inspection) => (
                <div
                  key={i.inspection_id}
                  className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0"
                >
                  <Badge tone="warn">{i.priority ?? "–"}</Badge>
                  <span className="flex-1 truncate">{i.message}</span>
                  <span className="shrink-0 font-mono text-[11px] text-txt-3">{i.zone ?? "–"}</span>
                </div>
              ))
            ) : (
              <Empty>No inspections requested.</Empty>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function AlertRow({ alert }: { alert: Alert }) {
  return (
    <div className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0">
      <SeverityChip severity={alert.severity} />
      <StateChip state={alert.state} />
      <span className="flex-1 truncate text-txt">{alert.message || alert.event_type}</span>
      <span className="shrink-0 text-[11px] text-txt-3">{timeAgo(alert.last_seen)}</span>
    </div>
  );
}
