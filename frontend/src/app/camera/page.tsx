"use client";

/**
 * Browser-camera monitoring.
 *
 * The camera belongs to this browser, not the server, so frames are captured here and shipped to
 * the edge over a WebSocket. The edge runs the *same* pipeline it uses for a server camera and
 * replies with detections, which are drawn over the live video. That is what lets FieldPilot run
 * on a laptop or phone with no camera attached to the server at all.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Badge, Button, Card, Empty, ErrorState, Note, SectionTitle } from "@/components/ui";

type Detection = {
  class: string;
  category: "person" | "ppe" | "equipment";
  ppe_item?: string | null;
  kind?: string | null;
  confidence: number | null;
  box: [number, number, number, number];
  track_id?: number | null;
  is_violation?: boolean;
};

type Pose = { track_id: number | null; keypoints: [number, number, number][]; visible: number };

type Hazard = {
  id: string;
  type: string;
  severity: string;
  message: string;
  track_id: number | null;
  bbox: [number, number, number, number] | null;
  ts_wall: number;
};

type FrameReply = {
  frame?: { index: number; width: number; height: number };
  detections?: Detection[];
  poses?: Pose[];
  counts?: { people: number; ppe_items: number; violations: number; poses: number };
  inference_ms?: number;
  hazards?: Hazard[];
  dropped?: number;
  error?: string;
};

/** COCO-17 skeleton edges, as produced by the pose model. */
const SKELETON: [number, number][] = [
  [0, 1], [0, 2], [1, 3], [2, 4], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10],
  [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
];

const KP_MIN = 0.3;
/** `useSyncExternalStore` subscribe for a value that cannot change during the page's life. */
const NEVER_CHANGES = () => () => {};

const COLOURS = {
  violation: "#f43f5e",
  person: "#38bdf8",
  ppe: "#22c55e",
  equipment: "#f59e0b",
  skeleton: "#a78bfa",
} as const;

function edgeSocketUrl(): string {
  const override = process.env.NEXT_PUBLIC_FIELDPILOT_EDGE_WS;
  if (override) return override;
  const host = typeof window === "undefined" ? "localhost" : window.location.hostname;
  const scheme = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${host}:8000/ws/video`;
}

export default function CameraPage() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const overlayRef = useRef<HTMLCanvasElement | null>(null);
  const grabRef = useRef<HTMLCanvasElement | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number | null>(null);
  const inFlightRef = useRef(false);

  const [running, setRunning] = useState(false);
  const [connected, setConnected] = useState(false);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState<string>("");
  const [fps, setFps] = useState(8);
  const [stats, setStats] = useState<FrameReply | null>(null);
  const [hazards, setHazards] = useState<Hazard[]>([]);
  const [error, setError] = useState<string | null>(null);

  // getUserMedia is gated to secure contexts; localhost counts, plain HTTP elsewhere does not.
  // Read through useSyncExternalStore rather than an effect: the value never changes, and the
  // server snapshot keeps SSR output identical to the first client render (no hydration mismatch).
  const secure = useSyncExternalStore(
    NEVER_CHANGES,
    () => window.isSecureContext || window.location.hostname === "localhost",
    () => true,
  );

  const draw = useCallback((reply: FrameReply) => {
    const canvas = overlayRef.current;
    const video = videoRef.current;
    if (!canvas || !video || !reply.frame) return;
    const { width, height } = reply.frame;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, width, height);

    for (const pose of reply.poses ?? []) {
      ctx.strokeStyle = COLOURS.skeleton;
      ctx.lineWidth = Math.max(1.5, width / 480);
      for (const [a, b] of SKELETON) {
        const p = pose.keypoints[a];
        const q = pose.keypoints[b];
        if (!p || !q || p[2] < KP_MIN || q[2] < KP_MIN) continue;
        ctx.beginPath();
        ctx.moveTo(p[0], p[1]);
        ctx.lineTo(q[0], q[1]);
        ctx.stroke();
      }
      ctx.fillStyle = COLOURS.skeleton;
      for (const k of pose.keypoints) {
        if (k[2] < KP_MIN) continue;
        ctx.beginPath();
        ctx.arc(k[0], k[1], Math.max(2, width / 320), 0, Math.PI * 2);
        ctx.fill();
      }
    }

    for (const d of reply.detections ?? []) {
      const colour = d.is_violation
        ? COLOURS.violation
        : d.category === "person"
          ? COLOURS.person
          : d.category === "equipment"
            ? COLOURS.equipment
            : COLOURS.ppe;
      const [x1, y1, x2, y2] = d.box;
      ctx.strokeStyle = colour;
      ctx.lineWidth = d.is_violation ? Math.max(3, width / 260) : Math.max(1.5, width / 420);
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

      const label =
        `${d.is_violation ? "⚠ " : ""}${d.class}` +
        (d.track_id != null ? ` #${d.track_id}` : "") +
        (d.confidence != null ? ` ${(d.confidence * 100).toFixed(0)}%` : "");
      ctx.font = `${Math.max(11, Math.round(width / 55))}px ui-sans-serif, system-ui, sans-serif`;
      const pad = 4;
      const w = ctx.measureText(label).width + pad * 2;
      const h = Math.max(16, Math.round(width / 42));
      ctx.fillStyle = colour;
      ctx.fillRect(x1, Math.max(0, y1 - h), w, h);
      ctx.fillStyle = "#0b0b0f";
      ctx.fillText(label, x1 + pad, Math.max(h - pad - 1, y1 - pad - 1));
    }
  }, []);

  const stop = useCallback(() => {
    if (timerRef.current != null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket) {
      socket.onopen = socket.onclose = socket.onerror = socket.onmessage = null;
      if (socket.readyState <= WebSocket.OPEN) socket.close();
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    inFlightRef.current = false;
    setConnected(false);
    setRunning(false);
  }, []);

  // stop() on unmount so the camera light never stays on after navigating away
  useEffect(() => stop, [stop]);

  const sendFrame = useCallback(() => {
    const socket = socketRef.current;
    const video = videoRef.current;
    const grab = grabRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN || !video || !grab) return;
    if (!video.videoWidth || inFlightRef.current) return;
    // one frame outstanding at a time: the server drops stale frames anyway, and a backlog
    // would only add latency to a safety signal
    if (socket.bufferedAmount > 0) return;

    if (grab.width !== video.videoWidth) grab.width = video.videoWidth;
    if (grab.height !== video.videoHeight) grab.height = video.videoHeight;
    const ctx = grab.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, grab.width, grab.height);
    inFlightRef.current = true;
    grab.toBlob(
      (blob) => {
        const live = socketRef.current;
        if (!blob || !live || live.readyState !== WebSocket.OPEN) {
          inFlightRef.current = false;
          return;
        }
        blob.arrayBuffer().then((buf) => live.send(buf)).catch(() => undefined);
      },
      "image/jpeg",
      0.72,
    );
  }, []);

  const start = useCallback(async () => {
    setError(null);
    if (!navigator.mediaDevices?.getUserMedia) {
      setError(
        "This browser exposes no camera API. getUserMedia needs a secure context — use localhost or HTTPS.",
      );
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: deviceId
          ? { deviceId: { exact: deviceId } }
          : { facingMode: "environment", width: { ideal: 1280 } },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play().catch(() => undefined);
      }
      // labels are only populated once permission is granted
      setDevices((await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput"));
    } catch (err) {
      const name = err instanceof DOMException ? err.name : "";
      setError(
        name === "NotAllowedError"
          ? "Camera permission was denied. Allow camera access for this site and try again."
          : name === "NotFoundError"
            ? "No camera was found on this device."
            : `Could not open the camera: ${err instanceof Error ? err.message : String(err)}`,
      );
      return;
    }

    const socket = new WebSocket(edgeSocketUrl());
    socket.binaryType = "arraybuffer";
    socketRef.current = socket;
    socket.onopen = () => {
      setConnected(true);
      setRunning(true);
      timerRef.current = window.setInterval(sendFrame, Math.round(1000 / fps));
    };
    socket.onmessage = (event) => {
      inFlightRef.current = false;
      let reply: FrameReply;
      try {
        reply = JSON.parse(String(event.data)) as FrameReply;
      } catch {
        return;
      }
      if (reply.error) {
        setError(reply.error);
        return;
      }
      setError(null);
      setStats(reply);
      draw(reply);
      if (reply.hazards?.length) {
        setHazards((prev) => [...reply.hazards!, ...prev].slice(0, 40));
      }
    };
    socket.onerror = () =>
      setError(
        `Could not reach the edge service at ${edgeSocketUrl()}. Start it with \`make edge\` or \`make run-all\`.`,
      );
    socket.onclose = () => {
      setConnected(false);
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [deviceId, draw, fps, sendFrame]);

  // retune the capture interval without tearing the socket down
  useEffect(() => {
    if (!running || timerRef.current == null) return;
    window.clearInterval(timerRef.current);
    timerRef.current = window.setInterval(sendFrame, Math.round(1000 / fps));
  }, [fps, running, sendFrame]);

  const counts = stats?.counts;

  return (
    <div className="p-6">
      <PageHeader
        title="Browser camera"
        subtitle="Capture from this device's camera — no camera needed on the server"
        action={
          <div className="flex items-center gap-2">
            <Badge tone={connected ? "good" : "neutral"}>
              {connected ? "streaming" : running ? "connecting…" : "stopped"}
            </Badge>
            <Button onClick={running ? stop : start} tone={running ? "secondary" : "primary"}>
              {running ? "Stop" : "Start camera"}
            </Button>
          </div>
        }
      />

      {!secure && (
        <Note tone="warn">
          This page is not in a secure context, so the browser will refuse camera access. Open it on{" "}
          <code className="font-mono">localhost</code>, or serve the dashboard over HTTPS.
        </Note>
      )}
      {error && <ErrorState message={error} onRetry={running ? undefined : start} />}

      <div className="mt-4 grid gap-4 lg:grid-cols-[2fr_1fr]">
        <div>
          <div className="relative aspect-video overflow-hidden rounded-xl border border-line bg-black">
            <video
              ref={videoRef}
              playsInline
              muted
              className="h-full w-full object-contain"
              aria-label="live camera preview"
            />
            <canvas
              ref={overlayRef}
              className="pointer-events-none absolute inset-0 h-full w-full object-contain"
            />
            {!running && (
              <div className="absolute inset-0 grid place-items-center text-sm text-zinc-400">
                <div className="text-center">
                  <div className="mb-2 text-2xl">◉</div>
                  <div className="font-semibold">Camera idle</div>
                  <div className="mt-1 text-xs text-zinc-500">
                    Press “Start camera” and allow access.
                  </div>
                </div>
              </div>
            )}
          </div>
          <canvas ref={grabRef} className="hidden" aria-hidden="true" />

          <Card className="mt-4 px-4 py-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block text-xs">
                <span className="mb-1 block font-medium text-txt-2">Camera</span>
                <select
                  value={deviceId}
                  onChange={(e) => setDeviceId(e.target.value)}
                  className="w-full rounded-lg border border-line bg-panel px-2.5 py-1.5 text-sm"
                >
                  <option value="">Default (rear where available)</option>
                  {devices.map((d, i) => (
                    <option key={d.deviceId || i} value={d.deviceId}>
                      {d.label || `Camera ${i + 1}`}
                    </option>
                  ))}
                </select>
                {running && (
                  <span className="mt-1 block text-[11px] text-txt-3">
                    Stop and start again to switch camera.
                  </span>
                )}
              </label>
              <label className="block text-xs">
                <span className="mb-1 block font-medium text-txt-2">
                  Capture rate — {fps} fps
                </span>
                <input
                  type="range"
                  min={2}
                  max={15}
                  step={1}
                  value={fps}
                  onChange={(e) => setFps(Number(e.target.value))}
                  className="w-full"
                />
                <span className="mt-1 block text-[11px] text-txt-3">
                  Higher is smoother but costs server inference.
                </span>
              </label>
            </div>
          </Card>
        </div>

        <div>
          <SectionTitle>This stream</SectionTitle>
          <Card className="px-4 py-3">
            <dl className="grid grid-cols-2 gap-y-2 text-sm">
              <dt className="text-txt-3">People</dt>
              <dd className="text-right font-semibold">{counts?.people ?? "–"}</dd>
              <dt className="text-txt-3">PPE items</dt>
              <dd className="text-right font-semibold">{counts?.ppe_items ?? "–"}</dd>
              <dt className="text-txt-3">Violations</dt>
              <dd
                className={`text-right font-semibold ${counts?.violations ? "text-rose-500" : ""}`}
              >
                {counts?.violations ?? "–"}
              </dd>
              <dt className="text-txt-3">Inference</dt>
              <dd className="text-right font-semibold">
                {stats?.inference_ms != null ? `${stats.inference_ms} ms` : "–"}
              </dd>
              <dt className="text-txt-3">Frames dropped</dt>
              <dd className="text-right font-semibold">{stats?.dropped ?? 0}</dd>
            </dl>
            <p className="mt-3 border-t border-line-soft pt-2 text-[11px] leading-relaxed text-txt-3">
              Hazards detected here go onto the same event bus as the server camera, so they reach
              the rules engine, alerts and notifications identically.
            </p>
          </Card>

          <SectionTitle>Hazards from this camera</SectionTitle>
          <Card>
            {hazards.length ? (
              hazards.map((h) => (
                <div
                  key={h.id}
                  className="border-b border-line-soft px-4 py-2.5 text-sm last:border-0"
                >
                  <div className="flex items-center gap-2">
                    <Badge tone={h.severity === "high" ? "bad" : "warn"}>{h.severity}</Badge>
                    <span className="font-medium">{h.type.replace(/_/g, " ")}</span>
                    {h.track_id != null && (
                      <span className="text-[11px] text-txt-3">#{h.track_id}</span>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-txt-2">{h.message}</div>
                </div>
              ))
            ) : (
              <Empty>No hazards detected from this camera yet.</Empty>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
