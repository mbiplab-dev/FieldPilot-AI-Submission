"use client";

import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { InspectionToggle } from "@/components/InspectionToggle";
import {
  Badge,
  Button,
  Card,
  Empty,
  Field,
  LiveChip,
  Note,
  SectionTitle,
  SeverityChip,
  inputClass,
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
  learning: "accent",
  zone: "neutral",
};

export default function LivePage() {
  const [feedUp, setFeedUp] = useState(true);
  const [zoneInput, setZoneInput] = useState("");
  const [zone, setZone] = useState("");

  const live = useLiveFeed({ zone: zone || undefined, bufferSize: 80 });
  const { data, error } = usePoll(
    () => api.alerts({ limit: "200" }),
    live.connected ? 20000 : 4000,
    [live.connected],
  );

  const act = (data?.alerts ?? []).filter((a) => a.state === "NEW" || a.state === "ACTIVE");

  return (
    <div className="p-6">
      <PageHeader
        title="Live feed"
        subtitle="Annotated edge stream · enable inspection mode to scan for structural defects"
        action={
          <div className="flex items-center gap-2.5">
            <LiveChip connected={live.connected} />
            <InspectionToggle variant="button" />
          </div>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <div>
          <div className="relative aspect-video overflow-hidden rounded-xl border border-line bg-black">
            {feedUp ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src="/feed/stream"
                alt="live edge feed"
                className="h-full w-full object-contain"
                onError={() => setFeedUp(false)}
              />
            ) : (
              <div className="grid h-full place-items-center text-sm text-zinc-400">
                <div className="text-center">
                  <div className="mb-2 text-2xl">⏻</div>
                  <div className="font-semibold">Live feed unavailable</div>
                  <div className="mt-1 text-xs text-zinc-500">
                    Start the edge: <code className="font-mono">make run-all</code> or{" "}
                    <code className="font-mono">make edge</code>
                  </div>
                  <div className="mt-3">
                    <Button size="sm" tone="secondary" onClick={() => setFeedUp(true)}>
                      Retry stream
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>

          <Card className="mt-4 px-4 py-4">
            <InspectionToggle variant="switch" />
          </Card>

          <SectionTitle>Live event ticker</SectionTitle>
          {!live.connected ? (
            <Note tone="warn" title="Not receiving push events">
              {live.error ?? "Connecting to the broadcast socket…"} The alert list on the right
              still refreshes on a timer.
            </Note>
          ) : null}
          <Card>
            <div className="flex flex-wrap items-end gap-3 border-b border-line-soft px-4 py-3">
              <Field label="Zone filter" htmlFor="live-zone" className="w-56">
                <input
                  id="live-zone"
                  value={zoneInput}
                  onChange={(e) => setZoneInput(e.target.value)}
                  placeholder="all zones"
                  className={inputClass}
                />
              </Field>
              <Button size="sm" tone="secondary" onClick={() => setZone(zoneInput.trim())}>
                Apply
              </Button>
              {zone ? (
                <Button
                  size="sm"
                  tone="secondary"
                  onClick={() => {
                    setZone("");
                    setZoneInput("");
                  }}
                >
                  Clear
                </Button>
              ) : null}
              <div className="ml-auto flex items-center gap-2 pb-1.5">
                <span className="text-[11px] text-txt-3">{live.frames.length} event(s)</span>
                <Button size="sm" tone="secondary" onClick={live.clear} disabled={!live.frames.length}>
                  Clear
                </Button>
              </div>
            </div>

            <ul aria-live="polite" aria-label="Live site events" className="max-h-[420px] overflow-y-auto">
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

        <div>
          <SectionTitle>Active right now</SectionTitle>
          <Card>
            {error && !data ? (
              <Empty>Alert list unavailable — {error}</Empty>
            ) : act.length ? (
              act.map((a: Alert) => (
                <div
                  key={a.alert_id}
                  className="flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0"
                >
                  <SeverityChip severity={a.severity} />
                  <span className="flex-1 truncate">{a.message || a.event_type}</span>
                  <span className="shrink-0 text-[11px] text-txt-3">{timeAgo(a.last_seen)}</span>
                </div>
              ))
            ) : (
              <Empty>No active alerts.</Empty>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

/**
 * Advisories are zone-scoped, cross-worker warnings ("three workers unharnessed
 * on level 4") rather than single detections — they get their own treatment.
 */
function TickerRow({ frame }: { frame: LiveFrame }) {
  const advisory = frame.topic === "advisory";
  return (
    <li
      className={`flex items-center gap-2.5 border-b border-line-soft px-4 py-2.5 text-sm last:border-0 ${
        advisory ? "border-l-2 border-l-purple-500 bg-purple-500/5" : ""
      }`}
    >
      <span className="w-[68px] shrink-0 font-mono text-[11px] text-txt-3">{fmtTime(frame.ts)}</span>
      <Badge tone={TOPIC_TONE[frame.topic] ?? "neutral"}>
        {advisory ? "advisory" : frame.topic.replace("_", " ")}
      </Badge>
      <span className={`flex-1 truncate ${advisory ? "font-semibold" : ""}`}>
        {frameSummary(frame)}
      </span>
      <span className="shrink-0 font-mono text-[11px] text-txt-3">{frame.zone ?? "–"}</span>
    </li>
  );
}
