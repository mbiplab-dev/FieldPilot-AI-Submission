"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";
import type { Alert, LearningRun, RFI } from "@/lib/api";

/**
 * Resilient client for the backend broadcast socket (`ws://<host>:8100/ws`).
 *
 * The socket is opened *directly* against the backend — Next.js rewrites do not
 * proxy websockets — so the URL is derived from `window.location` with a
 * `NEXT_PUBLIC_FIELDPILOT_WS` override for non-local deployments.
 *
 * Live push is an optimisation, never a requirement: every consumer keeps a
 * polling fallback and uses `connected` to show a degraded indicator.
 */

export const LIVE_TOPICS = [
  "alert",
  "alert_resolved",
  "notification",
  "advisory",
  "rfi",
  "inspection",
  "learning",
  "zone",
] as const;

export type LiveTopic = (typeof LIVE_TOPICS)[number];

export interface LiveFrame {
  /** One of {@link LIVE_TOPICS} — kept as `string` because the server may add more. */
  topic: string;
  zone: string | null;
  /** Server timestamp, seconds since epoch. */
  ts: number;
  data: unknown;
  /** Client-side monotonic id — a stable React key. */
  seq: number;
}

export interface UseLiveFeedOptions {
  /** `dashboard` (default) or `device`. */
  kind?: string;
  /** Restrict the server-side stream to a single zone. */
  zone?: string;
  /** Only keep these topics in `frames` / `last` and only call `onFrame` for them. */
  topics?: readonly string[];
  /** How many frames to retain, newest first. Default 60. */
  bufferSize?: number;
  /** Called for every retained frame — e.g. to trigger a refetch. */
  onFrame?: (frame: LiveFrame) => void;
  /** Set to `false` to keep the socket closed. Default `true`. */
  enabled?: boolean;
}

export interface LiveFeed {
  connected: boolean;
  /** True while the socket is trying to (re)connect. */
  connecting: boolean;
  /** Newest retained frame, or `null`. */
  last: LiveFrame | null;
  /** Retained frames, newest first. */
  frames: LiveFrame[];
  /** Human-readable reason the socket is down, or `null`. */
  error: string | null;
  /** Consecutive failed connection attempts. */
  attempts: number;
  clear: () => void;
}

const DEFAULT_PORT = "8100";
const PING_INTERVAL_MS = 25_000;
const MAX_BACKOFF_MS = 15_000;

/** Monotonic frame counter — only ever touched from socket callbacks. */
let frameSeq = 0;

export function liveFeedUrl(kind: string, zone?: string): string {
  const override = process.env.NEXT_PUBLIC_FIELDPILOT_WS;
  let base: string;
  if (override && override.length > 0) {
    base = override;
  } else if (typeof window === "undefined") {
    base = `ws://localhost:${DEFAULT_PORT}/ws`;
  } else {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    base = `${scheme}://${window.location.hostname}:${DEFAULT_PORT}/ws`;
  }
  const query = new URLSearchParams({ kind });
  if (zone) query.set("zone", zone);
  return `${base}${base.includes("?") ? "&" : "?"}${query.toString()}`;
}

function decodeFrame(raw: string): LiveFrame | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const rec = parsed as Record<string, unknown>;
  const topic = typeof rec.topic === "string" ? rec.topic : null;
  // `pong` is a keep-alive answer, not site activity.
  if (!topic || topic === "pong") return null;
  frameSeq += 1;
  return {
    topic,
    zone: typeof rec.zone === "string" ? rec.zone : null,
    ts: typeof rec.ts === "number" ? rec.ts : Date.now() / 1000,
    data: rec.data ?? {},
    seq: frameSeq,
  };
}

export function useLiveFeed(options: UseLiveFeedOptions = {}): LiveFeed {
  const { kind = "dashboard", zone, enabled = true, bufferSize = 60 } = options;

  const [frames, setFrames] = useState<LiveFrame[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempts, setAttempts] = useState(0);

  // Non-reactive: reads the latest `topics`/`onFrame` without reopening the socket.
  const handleFrame = useEffectEvent((frame: LiveFrame) => {
    const allowed = options.topics;
    if (allowed && allowed.length > 0 && !allowed.includes(frame.topic)) return;
    setFrames((prev) => [frame, ...prev].slice(0, Math.max(1, bufferSize)));
    options.onFrame?.(frame);
  });

  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;

    const url = liveFeedUrl(kind, zone);
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let pingTimer: ReturnType<typeof setInterval> | null = null;
    let attempt = 0;
    let everConnected = false;

    /** Detaches handlers first so a deliberate close never schedules a reconnect. */
    const teardownSocket = () => {
      if (pingTimer !== null) {
        clearInterval(pingTimer);
        pingTimer = null;
      }
      const current = socket;
      socket = null;
      if (!current) return;
      current.onopen = null;
      current.onclose = null;
      current.onerror = null;
      current.onmessage = null;
      if (
        current.readyState === WebSocket.OPEN ||
        current.readyState === WebSocket.CONNECTING
      ) {
        current.close();
      }
    };

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== null) return;
      attempt += 1;
      setAttempts(attempt);
      const backoff = Math.min(MAX_BACKOFF_MS, 500 * 2 ** Math.min(attempt - 1, 5));
      reconnectTimer = setTimeout(open, backoff + Math.random() * 250);
    };

    // Declared as a hoisted function so `scheduleReconnect` above can reference it.
    function open() {
      if (disposed) return;
      reconnectTimer = null;

      let next: WebSocket;
      try {
        next = new WebSocket(url);
      } catch {
        setConnected(false);
        setError("Live socket could not be opened.");
        scheduleReconnect();
        return;
      }
      socket = next;

      next.onopen = () => {
        if (disposed) return;
        attempt = 0;
        everConnected = true;
        setAttempts(0);
        setConnected(true);
        setError(null);
        pingTimer = setInterval(() => {
          if (next.readyState === WebSocket.OPEN) {
            next.send(JSON.stringify({ type: "ping" }));
          }
        }, PING_INTERVAL_MS);
      };

      next.onmessage = (event: MessageEvent<unknown>) => {
        if (disposed || typeof event.data !== "string") return;
        const frame = decodeFrame(event.data);
        if (frame) handleFrame(frame);
      };

      // `onerror` is always followed by `onclose`, so recovery lives there.
      next.onerror = () => {
        if (disposed) return;
        setConnected(false);
      };

      next.onclose = () => {
        if (disposed) return;
        teardownSocket();
        setConnected(false);
        setError(
          everConnected
            ? "Live updates disconnected — falling back to polling."
            : `Live socket at ${url} is not answering — falling back to polling.`,
        );
        scheduleReconnect();
      };
    }

    open();

    return () => {
      disposed = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      teardownSocket();
    };
  }, [enabled, kind, zone]);

  const clear = useCallback(() => setFrames([]), []);

  return {
    connected,
    connecting: enabled && !connected,
    last: frames[0] ?? null,
    frames,
    error: connected ? null : error,
    attempts,
    clear,
  };
}

/* --------------------------- frame payload guards --------------------------- */

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function frameAlert(frame: LiveFrame): Alert | null {
  const rec = record(frame.data);
  return rec && typeof rec.alert_id === "string" ? (frame.data as Alert) : null;
}

export function frameLearningRun(frame: LiveFrame): LearningRun | null {
  const rec = record(frame.data);
  return rec && typeof rec.run_id === "string" ? (frame.data as LearningRun) : null;
}

export function frameRfi(frame: LiveFrame): RFI | null {
  const rec = record(frame.data);
  return rec && typeof rec.rfi_id === "string" ? (frame.data as RFI) : null;
}

/** Best-effort one-line description of a frame, for tickers. */
export function frameSummary(frame: LiveFrame): string {
  const rec = record(frame.data);
  if (!rec) return frame.topic;
  for (const key of ["message", "subject", "title", "summary", "text", "name", "status"]) {
    const value = rec[key];
    if (typeof value === "string" && value.trim().length > 0) return value;
  }
  const eventType = rec.event_type;
  if (typeof eventType === "string") return eventType;
  return frame.topic;
}
