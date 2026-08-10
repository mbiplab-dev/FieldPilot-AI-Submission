"use client";

import { useEffect, useState } from "react";
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
  const [enlarged, setEnlarged] = useState<string | null>(null);

  // The enlarged feed follows the live list: if that worker stops streaming, close rather than
  // leave the manager staring at a frozen frame behind a modal.
  const expanded = live.find((f) => f.worker_id === enlarged) ?? null;

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
        <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
          {live.map((feed) => (
            <WorkerFeedCard
              key={feed.worker_id}
              feed={feed}
              onEnlarge={() => setEnlarged(feed.worker_id)}
            />
          ))}
        </div>
      )}

      {expanded ? (
        <FeedLightbox feed={expanded} onClose={() => setEnlarged(null)} />
      ) : null}
    </>
  );
}

/** Full-screen view of one camera, for when a manager needs to actually see what is happening. */
function FeedLightbox({ feed, onClose }: { feed: WorkerCameraFeed; onClose: () => void }) {
  // Escape closes, which is what anyone will try first on a full-screen overlay.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Enlarged camera from ${feed.display_name ?? feed.worker_id}`}
      className="fixed inset-0 z-50 flex flex-col bg-black/90 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div className="mb-3 flex items-center gap-3 text-white">
        <span className="flex items-center gap-1.5 rounded-full bg-red-600/90 px-2.5 py-1 text-[11px] font-bold">
          <span className="h-2 w-2 rounded-full bg-white" />
          LIVE
        </span>
        <span className="text-sm font-semibold">{feed.display_name ?? feed.worker_id}</span>
        <span className="font-mono text-xs text-zinc-400">{feed.worker_id}</span>
        <span className="text-xs text-zinc-400">{feed.zone ?? "no zone"}</span>
        <span className="text-xs text-zinc-400">{feed.fps.toFixed(1)} fps</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded-lg border border-zinc-600 px-3 py-1.5 text-xs font-medium text-zinc-200 hover:bg-zinc-800"
        >
          Close (Esc)
        </button>
      </div>

      {/* Stop the click on the image itself from closing — only the backdrop should. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={workerStreamUrl(feed.worker_id)}
        alt={`Live camera from ${feed.display_name ?? feed.worker_id}`}
        onClick={(e) => e.stopPropagation()}
        className="min-h-0 flex-1 rounded-lg object-contain"
      />
    </div>
  );
}

function WorkerFeedCard({
  feed,
  onEnlarge,
}: {
  feed: WorkerCameraFeed;
  onEnlarge: () => void;
}) {
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

        {broken ? null : (
          <button
            type="button"
            onClick={onEnlarge}
            aria-label={`Enlarge ${feed.display_name ?? feed.worker_id}'s camera`}
            title="Enlarge"
            className="absolute right-2 top-2 grid h-7 w-7 place-items-center rounded-lg bg-black/60 text-white transition-colors hover:bg-black/80"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" />
            </svg>
          </button>
        )}
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
