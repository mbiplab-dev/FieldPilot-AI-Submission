"use client";

import { PageHeader } from "@/components/PageHeader";
import { InspectionToggle } from "@/components/InspectionToggle";
import { WorkerFeeds } from "@/components/WorkerFeeds";
import {
  Badge,
  Button,
  Card,
  Empty,
  LiveChip,
  SectionTitle,
  SeverityChip,
  type Tone,
} from "@/components/ui";
import { api, fmtTime, timeAgo, type Alert } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { frameSummary, useLiveFeed, type LiveFrame } from "@/lib/useLiveFeed";

const TOPIC_TONE: Record<string, Tone> = {
  alert: "bad",
  alert_resolved: "good",
  notification: "info",
  advisory: "purple",
  rfi: "accent",
  inspection: "warn",
  question: "info",
  message: "purple",
  zone: "neutral",
};

/**
 * The site manager's wall: every worker's camera on the left, everything happening on the right.
 *
 * The server's own camera is deliberately absent. The workers' phones are the capture source now,
 * and a fixed webcam pointed at whatever the laptop faces is not site footage — showing it beside
 * real worker feeds implied a coverage that did not exist.
 */
export default function LivePage() {
  const live = useLiveFeed({ bufferSize: 120 });

  const { data, error } = usePoll(
    () => api.alerts({ limit: "200" }),
    live.connected ? 20000 : 4000,
    [live.connected],
  );

  const active = (data?.alerts ?? []).filter((a) => a.state === "NEW" || a.state === "ACTIVE");

  return (
    <div className="p-6">
      <PageHeader
        title="Live site"
        subtitle="Every worker's camera, analysed on the server · alerts and AI verdicts as they happen"
        action={
          <div className="flex items-center gap-2.5">
            <LiveChip connected={live.connected} />
            <InspectionToggle variant="button" />
          </div>
        }
      />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
        {/* ---------------------------------------------------------- cameras */}
        <div className="min-w-0">
          <WorkerFeeds />
        </div>

        {/* ---------------------------------------------------------- activity */}
        <div className="min-w-0">
          <SectionTitle>Active alerts</SectionTitle>
          <Card className="mb-5">
            {error && !data ? (
              <Empty>Alert list unavailable — {error}</Empty>
            ) : active.length ? (
              <div className="max-h-[280px] overflow-y-auto">
                {active.map((a: Alert) => (
                  <AlertRow key={a.alert_id} alert={a} />
                ))}
              </div>
            ) : (
              <Empty>No active alerts.</Empty>
            )}
          </Card>

          <div className="flex items-center justify-between">
            <SectionTitle>Live activity &amp; AI verdicts</SectionTitle>
            <Button
              size="sm"
              tone="secondary"
              onClick={live.clear}
              disabled={!live.frames.length}
            >
              Clear
            </Button>
          </div>
          <Card>
            <ul
              aria-live="polite"
              aria-label="Live site events"
              className="max-h-[560px] overflow-y-auto"
            >
              {live.frames.length ? (
                live.frames.map((frame) => <TickerRow key={frame.seq} frame={frame} />)
              ) : (
                <li>
                  <Empty>
                    {live.connected
                      ? "Connected — waiting for the next site event."
                      : "No live events (socket down)."}
                  </Empty>
                </li>
              )}
            </ul>
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
      <span className="flex-1 truncate">{alert.message || alert.event_type}</span>
      <span className="shrink-0 text-[11px] text-txt-3">{timeAgo(alert.last_seen)}</span>
    </div>
  );
}

/**
 * One line of site activity.
 *
 * Advisories are zone-scoped cross-worker warnings rather than single detections, so they get their
 * own emphasis. Alerts additionally show the LLM's verdict where there is one — that verdict is
 * noise control, never a safety authority, so a hazard the model disputed is surfaced loudly rather
 * than quietly dropped.
 */
function TickerRow({ frame }: { frame: LiveFrame }) {
  const advisory = frame.topic === "advisory";
  const data = frame.data as Record<string, unknown> | null;
  const verdict =
    data && typeof data === "object" ? (data.llm_verdict as Record<string, unknown> | undefined) : undefined;
  const disputed = Boolean(data?.llm_disputed ?? verdict?.disputed);
  const spoken = typeof data?.speech === "string" ? (data.speech as string) : null;
  const reasoning = typeof verdict?.reasoning === "string" ? (verdict.reasoning as string) : null;

  return (
    <li
      className={`border-b border-line-soft px-4 py-2.5 text-sm last:border-0 ${
        advisory ? "border-l-2 border-l-purple-500 bg-purple-500/5" : ""
      }`}
    >
      <div className="flex items-center gap-2.5">
        <span className="w-[60px] shrink-0 font-mono text-[11px] text-txt-3">
          {fmtTime(frame.ts)}
        </span>
        <Badge tone={TOPIC_TONE[frame.topic] ?? "neutral"}>
          {advisory ? "advisory" : frame.topic.replace("_", " ")}
        </Badge>
        <span className={`flex-1 truncate ${advisory ? "font-semibold" : ""}`}>
          {frameSummary(frame)}
        </span>
        <span className="shrink-0 font-mono text-[11px] text-txt-3">{frame.zone ?? "–"}</span>
      </div>

      {spoken ? (
        <div className="mt-1 pl-[70px] text-[11px] text-txt-3">
          <span className="mr-1">🔊</span>
          {spoken}
        </div>
      ) : null}

      {reasoning ? (
        <div className="mt-1 flex items-start gap-1.5 pl-[70px]">
          {disputed ? (
            <Badge tone="bad">escalated anyway</Badge>
          ) : (
            <Badge tone="neutral">AI</Badge>
          )}
          <span className="flex-1 text-[11px] text-txt-3">{reasoning}</span>
        </div>
      ) : null}
    </li>
  );
}
