"use client";

import { useState } from "react";
import { Badge, Card, Empty, Note, SectionTitle } from "@/components/ui";
import { api, errorMessage, timeAgo, workerStreamUrl, type WorkerCameraFeed } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

/** Stable identity so the `usePoll` fallback does not invent a new array every render. */
const EMPTY: WorkerCameraFeed[] = [];

/**
 * Every worker whose phone is currently streaming, as a live grid.
 *
 * The frames are MJPEG straight from the edge server, already annotated with what the detectors
 * found — the phone sends raw pixels and does no inference, so the boxes here are the server's own
 * verdict rather than anything the handset decided.
 */
export function WorkerFeeds() {
  const { data, error, loading } = usePoll(() => api.workerFeeds(), 5000);
  const feeds = data?.feeds ?? EMPTY;
  const live = feeds.filter((f) => f.live);

  return (
    <>
      <SectionTitle>Worker cameras</SectionTitle>

      {error ? (
        <Note tone="warn" title="Could not reach the vision service">
          {errorMessage(error)} Worker feeds are served by the edge on :8000.
        </Note>
      ) : null}

      {live.length === 0 ? (
        <Card>
          <Empty>
            {loading
              ? "Looking for worker cameras…"
              : "No worker is streaming. A worker starts their feed from the Camera tab in the FieldPilot Worker app."}
          </Empty>
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {live.map((feed) => (
            <WorkerFeedCard key={feed.worker_id} feed={feed} />
          ))}
        </div>
      )}
    </>
  );
}

function WorkerFeedCard({ feed }: { feed: WorkerCameraFeed }) {
  // A cache-busting key so the browser opens a fresh MJPEG response after a reconnect rather than
  // reusing a response whose stream the server already ended.
  const [attempt, setAttempt] = useState(0);
  const [broken, setBroken] = useState(false);

  return (
    <Card className="overflow-hidden p-0">
      <div className="relative aspect-video bg-black">
        {broken ? (
          <button
            type="button"
            onClick={() => {
              setBroken(false);
              setAttempt((n) => n + 1);
            }}
            className="grid h-full w-full place-items-center text-xs text-zinc-400 hover:text-zinc-200"
          >
            Stream ended — click to reconnect
          </button>
        ) : (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={attempt}
            src={`${workerStreamUrl(feed.worker_id)}?t=${attempt}`}
            alt={`Live camera from ${feed.display_name ?? feed.worker_id}`}
            className="h-full w-full object-contain"
            onError={() => setBroken(true)}
          />
        )}

        <div className="absolute left-2 top-2 flex items-center gap-1.5 rounded-full bg-black/60 px-2 py-1">
          <span className="h-2 w-2 rounded-full bg-red-500" />
          <span className="text-[10px] font-bold tracking-wide text-white">LIVE</span>
        </div>
      </div>

      <div className="px-3.5 py-2.5">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-semibold text-txt">
            {feed.display_name ?? feed.worker_id}
          </span>
          <span className="font-mono text-[11px] text-txt-3">{feed.worker_id}</span>
          {feed.hazards > 0 ? (
            <Badge tone="bad">
              {feed.hazards} hazard{feed.hazards === 1 ? "" : "s"}
            </Badge>
          ) : null}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-txt-3">
          <span>{feed.zone ?? "no zone"}</span>
          <span>{feed.fps.toFixed(1)} fps</span>
          {feed.width > 0 ? (
            <span>
              {feed.width}×{feed.height}
            </span>
          ) : null}
          {feed.last_frame_at ? <span>updated {timeAgo(feed.last_frame_at)}</span> : null}
        </div>
      </div>
    </Card>
  );
}
