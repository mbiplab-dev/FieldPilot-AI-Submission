"use client";

import { useCallback, useState } from "react";
import Link from "next/link";
import { fmtTime } from "@/lib/api";
import { useLiveFeed, type LiveFrame } from "@/lib/useLiveFeed";

interface Alarm {
  id: string;
  message: string;
  zone: string | null;
  reporter: string;
  severity: string;
  at: number;
}

/** How many stacked alarms to show before older ones are dropped from view. */
const MAX_VISIBLE = 3;

/**
 * A worker deliberately raising the alarm, shown so the manager cannot miss it.
 *
 * A model detection is an inference; a person pressing "report hazard" is a human on site saying
 * *something is wrong right now*. That is a stronger signal and it earns a louder treatment than
 * another row in a list — so it interrupts, in red, wherever the manager happens to be.
 *
 * These are identified by `payload.reported_by`, which `POST /me/alerts` stamps on every
 * hand-raised report. Machine detections never carry it, so nothing else can trip this.
 */
export function WorkerAlarmBanner() {
  const [alarms, setAlarms] = useState<Alarm[]>([]);

  const dismiss = useCallback((id: string) => {
    setAlarms((prev) => prev.filter((a) => a.id !== id));
  }, []);

  useLiveFeed({
    topics: ["alert"],
    onFrame: (frame: LiveFrame) => {
      const data = frame.data as Record<string, unknown> | null;
      if (!data || typeof data !== "object") return;

      const payload = data.payload as Record<string, unknown> | undefined;
      const reportedBy = payload?.reported_by;
      // Only hand-raised reports. A model detection has its own, quieter path.
      if (typeof reportedBy !== "string" || !reportedBy) return;

      const id = typeof data.alert_id === "string" ? data.alert_id : `seq-${frame.seq}`;
      setAlarms((prev) => {
        if (prev.some((a) => a.id === id)) return prev;
        const next: Alarm = {
          id,
          message:
            (typeof data.message === "string" && data.message) ||
            (typeof data.event_type === "string" ? data.event_type.replace(/_/g, " ") : "Hazard"),
          zone: typeof data.zone === "string" ? data.zone : null,
          reporter:
            (typeof payload?.reporter_name === "string" && payload.reporter_name) || reportedBy,
          severity: typeof data.severity === "string" ? data.severity : "high",
          at: frame.ts,
        };
        return [next, ...prev].slice(0, MAX_VISIBLE);
      });
    },
  });

  if (alarms.length === 0) return null;

  return (
    <div
      role="alert"
      aria-live="assertive"
      className="pointer-events-none fixed inset-x-0 top-0 z-40 flex flex-col items-center gap-2 p-3"
    >
      {alarms.map((alarm) => (
        <div
          key={alarm.id}
          className="pointer-events-auto flex w-full max-w-2xl items-start gap-3 rounded-xl border border-red-500/50 bg-red-600 px-4 py-3 text-white shadow-lg"
        >
          <span className="mt-0.5 shrink-0 animate-pulse" aria-hidden>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
              <path d="M12 9v4M12 17h.01" />
            </svg>
          </span>

          <div className="min-w-0 flex-1">
            <div className="text-[11px] font-bold uppercase tracking-wider text-red-100">
              Worker raised the alarm · {alarm.severity}
            </div>
            <div className="truncate text-sm font-semibold">{alarm.message}</div>
            <div className="text-[11px] text-red-100">
              {alarm.reporter}
              {alarm.zone ? ` · ${alarm.zone}` : ""} · {fmtTime(alarm.at)}
            </div>
          </div>

          <Link
            href="/alerts"
            onClick={() => dismiss(alarm.id)}
            className="shrink-0 rounded-lg bg-white/15 px-2.5 py-1.5 text-xs font-semibold hover:bg-white/25"
          >
            Open
          </Link>
          <button
            type="button"
            onClick={() => dismiss(alarm.id)}
            aria-label="Dismiss alarm"
            className="shrink-0 rounded-lg px-2 py-1.5 text-xs font-semibold text-red-100 hover:bg-white/15"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

/** Small red marker for a hand-raised report inside a list. */
export function ReportedByWorker({ name }: { name?: string | null }) {
  return (
    <span
      title={name ? `Reported by ${name}` : "Reported by a worker"}
      className="inline-flex items-center gap-1 rounded-full bg-red-500/15 px-1.5 py-0.5 text-[10px] font-bold text-red-500"
    >
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
        <path d="M12 9v4M12 17h.01" />
      </svg>
      WORKER
    </span>
  );
}
