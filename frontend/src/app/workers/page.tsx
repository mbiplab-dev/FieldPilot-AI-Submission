"use client";

import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import { Card, Empty, ErrorState, Loading, Note } from "@/components/ui";
import { api, type WorkerSummary } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

export default function WorkersPage() {
  const { data, error, loading, refresh } = usePoll(() => api.workers(), 5000);

  // sort by active alerts desc — copy first, never mutate the polled payload
  const workers = [...(data?.workers ?? [])].sort((a, b) => b.active_alerts - a.active_alerts);

  if (error && !data) {
    return (
      <div className="p-6">
        <PageHeader title="Workers" subtitle="Live status and safety score per worker" />
        <ErrorState message={error} onRetry={() => void refresh()} />
      </div>
    );
  }

  return (
    <div className="p-6">
      <PageHeader title="Workers" subtitle="Live status and safety score per worker · click for the full timeline" />
      {error ? (
        <Note tone="warn" title="Showing the last good snapshot">
          {error}
        </Note>
      ) : null}
      <Card>
        <table className="w-full">
          <thead>
            <tr className="border-b border-line text-left">
              <Th>Worker</Th>
              <Th>Zone</Th>
              <Th>Active alerts</Th>
              <Th>Open</Th>
            </tr>
          </thead>
          <tbody>
            {workers.length ? (
              workers.map((w: WorkerSummary) => (
                <tr
                  key={w.worker_id}
                  className="border-b border-line-soft transition-colors last:border-0 hover:bg-panel-2"
                >
                  <Td>
                    <Link href={`/workers/${encodeURIComponent(w.worker_id)}`} className="font-mono text-xs text-accent hover:underline">
                      {w.worker_id}
                    </Link>
                  </Td>
                  <Td><span className="font-mono text-xs text-txt-2">{w.zone ?? "–"}</span></Td>
                  <Td><span className="font-mono text-sm">{w.active_alerts}</span></Td>
                  <Td>
                    <Link href={`/workers/${encodeURIComponent(w.worker_id)}`} className="text-sm text-accent hover:underline">
                      View timeline →
                    </Link>
                  </Td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={4}>
                  {loading ? (
                    <Loading label="Loading workers…" />
                  ) : (
                    <Empty>
                      No workers seen yet — events carry worker_id when the edge tracks people.
                    </Empty>
                  )}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3.5 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-txt-3">{children}</th>;
}
function Td({ children }: { children: React.ReactNode }) {
  return <td className="px-3.5 py-2.5 text-[13px] align-middle">{children}</td>;
}