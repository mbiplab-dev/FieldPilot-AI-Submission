"use client";

import { useParams } from "next/navigation";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Card,
  Empty,
  ErrorState,
  Loading,
  Note,
  SectionTitle,
  SeverityChip,
  StateChip,
  StatusChip,
} from "@/components/ui";
import { api, fmtTime, type Alert, type PlatformEvent } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

export default function WorkerDetailPage() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params.id);
  const { data, error, refresh } = usePoll(() => api.workerTimeline(id), 6000, [id]);

  if (!data) {
    return (
      <div className="p-6">
        <PageHeader title={`Worker · ${id}`} />
        {error ? (
          <ErrorState message={error} onRetry={() => void refresh()} />
        ) : (
          <Card>
            <Loading label="Loading timeline…" />
          </Card>
        )}
      </div>
    );
  }

  const scoreColor =
    data.safety_score >= 90 ? "var(--color-emerald-500)" :
    data.safety_score >= 70 ? "var(--color-amber-500)" : "var(--color-red-500)";

  return (
    <div className="p-6">
      <PageHeader
        title={`Worker · ${data.worker_id}`}
        subtitle={`Zone ${data.current_zone ?? "–"} · status ${data.live_status.replace("_", " ")}`}
      />

      {error ? (
        <Note tone="warn" title="Showing the last good snapshot">
          {error}
        </Note>
      ) : null}

      <div className="mb-6 flex items-center gap-5">
        <div
          className="grid h-16 w-16 place-items-center rounded-full border-[3px] text-xl font-bold"
          style={{ borderColor: scoreColor, color: scoreColor }}
        >
          {data.safety_score}
        </div>
        <div>
          <StatusChip status={data.live_status} />
          <div className="mt-1 text-sm text-txt-2">safety score (low 0 / high 100)</div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="lg:col-span-2">
          <SectionTitle>Active alerts ({data.active_alerts.length})</SectionTitle>
          <Card>
            {data.active_alerts.length ? (
              data.active_alerts.map((a: Alert) => (
                <div key={a.alert_id} className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0">
                  <SeverityChip severity={a.severity} />
                  <span className="flex-1 truncate">{a.message || a.event_type}</span>
                  <span className="text-[11px] text-txt-3">{fmtTime(a.last_seen)}</span>
                </div>
              ))
            ) : (
              <Empty>None.</Empty>
            )}
          </Card>
        </div>

        <div>
          <SectionTitle>Past alerts ({data.past_alerts.length})</SectionTitle>
          <Card>
            {data.past_alerts.length ? (
              data.past_alerts.slice(0, 10).map((a: Alert) => (
                <div key={a.alert_id} className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0">
                  <StateChip state={a.state} />
                  <span className="flex-1 truncate">{a.message || a.event_type}</span>
                  <span className="text-[11px] text-txt-3">{fmtTime(a.last_seen)}</span>
                </div>
              ))
            ) : (
              <Empty>None.</Empty>
            )}
          </Card>
        </div>

        <div>
          <SectionTitle>Recent events ({data.recent_events.length})</SectionTitle>
          <Card>
            {data.recent_events.length ? (
              data.recent_events.slice(0, 10).map((ev: PlatformEvent) => (
                <div key={ev.event_id} className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0">
                  <Badge tone="info">{ev.event_type}</Badge>
                  <span className="flex-1 truncate font-mono text-xs text-txt-2">{ev.camera_id}</span>
                  <span className="text-[11px] text-txt-3">{fmtTime(ev.timestamp)}</span>
                </div>
              ))
            ) : (
              <Empty>None.</Empty>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}