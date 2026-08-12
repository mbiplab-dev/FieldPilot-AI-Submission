"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Button,
  Card,
  Empty,
  Field,
  Loading,
  Note,
  SectionTitle,
  inputClass,
} from "@/components/ui";
import {
  api,
  errorMessage,
  fmtDateTime,
  type CaptureBox,
  type CaptureFrame,
  type CaptureSession,
  type CaptureSplit,
  type DatasetExport,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

const FALLBACK_CLASSES = [
  "Hardhat",
  "Mask",
  "NO-Hardhat",
  "NO-Mask",
  "NO-Safety Vest",
  "Person",
  "Safety Cone",
  "Safety Vest",
  "machinery",
  "vehicle",
];
const BOX_COLORS = [
  "#60a5fa",
  "#22d3ee",
  "#fb7185",
  "#f97316",
  "#f59e0b",
  "#a78bfa",
  "#2dd4bf",
  "#4ade80",
  "#facc15",
  "#e879f9",
];

type Interaction = {
  mode: "draw" | "move" | "resize";
  start: [number, number];
  boxId?: string;
  original?: [number, number, number, number];
};

export default function DatasetPage() {
  const sessionsPoll = usePoll(() => api.captureSessions(), 5000);
  const feedsPoll = usePoll(() => api.workerFeeds(), 5000);
  const classesPoll = usePoll(() => api.captureClasses(), 60000);
  const sessions = useMemo(() => sessionsPoll.data?.sessions ?? [], [sessionsPoll.data]);
  const liveFeeds = useMemo(
    () => (feedsPoll.data?.feeds ?? []).filter((feed) => feed.live),
    [feedsPoll.data],
  );
  const classes = useMemo(
    () => classesPoll.data?.classes.map((item) => item.name) ?? FALLBACK_CLASSES,
    [classesPoll.data],
  );

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedFrameId, setSelectedFrameId] = useState<string | null>(null);
  const [selectedWorker, setSelectedWorker] = useState("");
  const [sessionName, setSessionName] = useState("");
  const [sessionSplit, setSessionSplit] = useState<CaptureSplit>("train");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: "good" | "bad" | "warn"; text: string } | null>(
    null,
  );
  const [lastExport, setLastExport] = useState<DatasetExport | null>(null);

  const activeSessionId = sessions.some((session) => session.session_id === selectedSessionId)
    ? selectedSessionId
    : (sessions[0]?.session_id ?? null);
  const activeWorker = liveFeeds.some((feed) => feed.worker_id === selectedWorker)
    ? selectedWorker
    : (liveFeeds[0]?.worker_id ?? "");

  const framesPoll = usePoll(
    () =>
      activeSessionId
        ? api.captureFrames(activeSessionId)
        : Promise.resolve({ frames: [] as CaptureFrame[] }),
    6000,
    [activeSessionId],
  );
  const frames = useMemo(() => framesPoll.data?.frames ?? [], [framesPoll.data]);
  const selectedSession =
    sessions.find((session) => session.session_id === activeSessionId) ?? null;
  const activeFrameId = frames.some((frame) => frame.frame_id === selectedFrameId)
    ? selectedFrameId
    : ((frames.find((frame) => frame.review_status === "draft") ?? frames[0])?.frame_id ?? null);
  const selectedFrame = frames.find((frame) => frame.frame_id === activeFrameId) ?? null;

  const createSession = async () => {
    if (!sessionName.trim()) return;
    setBusy("session");
    setMessage(null);
    try {
      const session = await api.createCaptureSession({
        name: sessionName.trim(),
        split: sessionSplit,
      });
      setSessionName("");
      await sessionsPoll.refresh();
      setSelectedSessionId(session.session_id);
      setSelectedFrameId(null);
      setMessage({
        tone: "good",
        text: session.name + " is ready for " + session.split + " captures.",
      });
    } catch (error) {
      setMessage({ tone: "bad", text: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  const capture = async () => {
    if (!activeSessionId || !activeWorker) return;
    setBusy("capture");
    setMessage(null);
    try {
      const snapshot = await api.workerCapture(activeWorker);
      const frame = await api.saveCapture(activeSessionId, snapshot);
      await Promise.all([framesPoll.refresh(), sessionsPoll.refresh()]);
      setSelectedFrameId(frame.frame_id);
      setMessage({ tone: "good", text: "Frame captured with editable model suggestions." });
    } catch (error) {
      setMessage({ tone: "bad", text: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  const exportDataset = async () => {
    setBusy("export");
    setMessage(null);
    try {
      const result = await api.exportCaptures(sessions.map((session) => session.session_id));
      setLastExport(result);
      setMessage({
        tone: "good",
        text: "Reviewed sessions exported without crossing split boundaries.",
      });
    } catch (error) {
      setMessage({ tone: "bad", text: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Training set"
        subtitle="Capture real phone frames · correct model suggestions · export session-safe YOLO labels"
        action={
          <Button
            onClick={() => void exportDataset()}
            disabled={busy !== null || sessions.length === 0}
          >
            {busy === "export" ? "Exporting…" : "Export reviewed"}
          </Button>
        }
      />

      {message ? <Note tone={message.tone}>{message.text}</Note> : null}
      {lastExport ? (
        <Note tone="good" title="Dataset ready">
          <code className="font-mono text-[12px]">{lastExport.data_yaml}</code>
        </Note>
      ) : null}

      <div className="grid gap-5 xl:grid-cols-[270px_minmax(0,1fr)]">
        <aside className="min-w-0">
          <SectionTitle>Recording sessions</SectionTitle>
          <Card className="overflow-hidden">
            <div className="border-line space-y-2 border-b p-3">
              <Field label="Session name" htmlFor="session-name">
                <input
                  id="session-name"
                  value={sessionName}
                  onChange={(event) => setSessionName(event.target.value)}
                  placeholder="North gate · morning"
                  className={inputClass}
                />
              </Field>
              <div className="grid grid-cols-[1fr_auto] gap-2">
                <select
                  value={sessionSplit}
                  onChange={(event) => setSessionSplit(event.target.value as CaptureSplit)}
                  aria-label="Dataset split"
                  className={inputClass}
                >
                  <option value="train">Train session</option>
                  <option value="val">Validation session</option>
                  <option value="test">Test session</option>
                </select>
                <Button
                  size="sm"
                  onClick={() => void createSession()}
                  disabled={!sessionName.trim() || busy !== null}
                >
                  Add
                </Button>
              </div>
              <p className="text-txt-3 text-[10.5px] leading-relaxed">
                Keep one recording session in one split. Use a different worker, location, or time
                for validation.
              </p>
            </div>
            {sessionsPoll.loading && !sessionsPoll.data ? (
              <Loading label="Loading sessions…" />
            ) : sessions.length ? (
              <div className="max-h-[560px] overflow-y-auto p-2">
                {sessions.map((session) => (
                  <SessionRow
                    key={session.session_id}
                    session={session}
                    selected={session.session_id === activeSessionId}
                    onSelect={() => {
                      setSelectedSessionId(session.session_id);
                      setSelectedFrameId(null);
                    }}
                  />
                ))}
              </div>
            ) : (
              <Empty>Create a training and a validation session to begin.</Empty>
            )}
          </Card>
        </aside>

        <main className="min-w-0">
          <SectionTitle>Capture from a live phone</SectionTitle>
          <Card className="mb-5 flex flex-wrap items-center gap-3 px-4 py-3">
            <div className="min-w-[220px] flex-1">
              <select
                value={activeWorker}
                onChange={(event) => setSelectedWorker(event.target.value)}
                aria-label="Live worker camera"
                className={inputClass}
              >
                {liveFeeds.length === 0 ? <option value="">No live phones</option> : null}
                {liveFeeds.map((feed) => (
                  <option key={feed.worker_id} value={feed.worker_id}>
                    {feed.display_name ?? feed.worker_id} · {feed.zone ?? "no zone"} ·{" "}
                    {feed.fps.toFixed(1)} fps
                  </option>
                ))}
              </select>
            </div>
            <div className="text-txt-3 text-[11px]">
              {selectedSession ? (
                <span>
                  Saving to <b className="text-txt-2">{selectedSession.name}</b> ·{" "}
                  {selectedSession.split}
                </span>
              ) : (
                "Choose a session first"
              )}
            </div>
            <Button
              onClick={() => void capture()}
              disabled={!activeSessionId || !activeWorker || busy !== null}
            >
              {busy === "capture" ? "Capturing…" : "Capture latest frame"}
            </Button>
          </Card>

          {framesPoll.error && !framesPoll.data ? (
            <Note tone="bad">{framesPoll.error}</Note>
          ) : selectedSession && frames.length ? (
            <>
              <SectionTitle>Session filmstrip</SectionTitle>
              <div className="mb-4 flex gap-2 overflow-x-auto pb-2">
                {frames.map((frame, index) => (
                  <FrameTab
                    key={frame.frame_id}
                    frame={frame}
                    index={index}
                    selected={frame.frame_id === activeFrameId}
                    onSelect={() => setSelectedFrameId(frame.frame_id)}
                  />
                ))}
              </div>
              {selectedFrame ? (
                <AnnotationEditor
                  key={selectedFrame.frame_id}
                  frame={selectedFrame}
                  classes={classes}
                  onSaved={async (updated) => {
                    setSelectedFrameId(updated.frame_id);
                    await Promise.all([framesPoll.refresh(), sessionsPoll.refresh()]);
                  }}
                />
              ) : null}
            </>
          ) : selectedSession ? (
            <Card>
              <Empty>
                No frames in this session. Start a worker phone stream, then capture varied moments.
              </Empty>
            </Card>
          ) : (
            <Card>
              <Empty>Create or choose a recording session.</Empty>
            </Card>
          )}
        </main>
      </div>
    </div>
  );
}

function SessionRow({
  session,
  selected,
  onSelect,
}: {
  session: CaptureSession;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`mb-1.5 w-full rounded-lg border px-3 py-2.5 text-left transition-colors last:mb-0 ${
        selected
          ? "border-accent bg-accent/10"
          : "hover:border-line hover:bg-panel-2 border-transparent"
      }`}
    >
      <div className="flex items-center gap-2">
        <span className="text-txt min-w-0 flex-1 truncate text-[12px] font-semibold">
          {session.name}
        </span>
        <Badge
          tone={
            session.split === "train" ? "accent" : session.split === "val" ? "purple" : "neutral"
          }
        >
          {session.split}
        </Badge>
      </div>
      <div className="text-txt-3 mt-1.5 flex items-center gap-2 font-mono text-[10px]">
        <span>{session.frame_count} frames</span>
        <span className="text-emerald-500">{session.reviewed_count} reviewed</span>
        {session.draft_count ? (
          <span className="text-amber-500">{session.draft_count} draft</span>
        ) : null}
      </div>
    </button>
  );
}

function FrameTab({
  frame,
  index,
  selected,
  onSelect,
}: {
  frame: CaptureFrame;
  index: number;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`group relative w-28 shrink-0 overflow-hidden rounded-lg border text-left transition-all ${
        selected ? "border-accent ring-accent/25 ring-2" : "border-line hover:border-txt-3"
      }`}
    >
      <CaptureImage
        frameId={frame.frame_id}
        className="aspect-video w-full bg-black object-cover"
        alt=""
      />
      <div className="bg-panel flex items-center justify-between px-2 py-1.5">
        <span className="text-txt-3 font-mono text-[10px]">
          F{String(index + 1).padStart(3, "0")}
        </span>
        <span
          className={`h-2 w-2 rounded-full ${frame.review_status === "reviewed" ? "bg-emerald-500" : "bg-amber-500"}`}
        />
      </div>
    </button>
  );
}

function CaptureImage({
  frameId,
  className,
  alt,
}: {
  frameId: string;
  className: string;
  alt: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    void api.captureImage(frameId).then((blob) => {
      if (!active) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [frameId]);
  return url ? (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={url} alt={alt} className={className} draggable={false} />
  ) : (
    <div className={`${className} bg-panel-2 animate-pulse`} aria-label="Loading captured frame" />
  );
}

function AnnotationEditor({
  frame,
  classes,
  onSaved,
}: {
  frame: CaptureFrame;
  classes: string[];
  onSaved: (frame: CaptureFrame) => Promise<void>;
}) {
  const [boxes, setBoxes] = useState<CaptureBox[]>(frame.boxes);
  const [selectedBoxId, setSelectedBoxId] = useState<string | null>(frame.boxes[0]?.box_id ?? null);
  const [activeClass, setActiveClass] = useState(frame.boxes[0]?.class_id ?? 5);
  const [interaction, setInteraction] = useState<Interaction | null>(null);
  const [drawing, setDrawing] = useState<[number, number, number, number] | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const dirty = useMemo(
    () => JSON.stringify(boxes) !== JSON.stringify(frame.boxes),
    [boxes, frame.boxes],
  );

  const point = (event: ReactPointerEvent): [number, number] => {
    const rect = overlayRef.current?.getBoundingClientRect();
    if (!rect) return [0, 0];
    return [
      Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    ];
  };

  const startDraw = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = point(event);
    event.currentTarget.setPointerCapture(event.pointerId);
    setSelectedBoxId(null);
    setInteraction({ mode: "draw", start });
    setDrawing([start[0], start[1], start[0], start[1]]);
  };

  const startBoxInteraction = (
    event: ReactPointerEvent,
    box: CaptureBox,
    mode: "move" | "resize",
  ) => {
    event.preventDefault();
    event.stopPropagation();
    overlayRef.current?.setPointerCapture(event.pointerId);
    setSelectedBoxId(box.box_id);
    setActiveClass(box.class_id);
    setInteraction({ mode, start: point(event), boxId: box.box_id, original: [...box.xyxy] });
  };

  const move = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!interaction) return;
    const current = point(event);
    if (interaction.mode === "draw") {
      setDrawing([interaction.start[0], interaction.start[1], current[0], current[1]]);
      return;
    }
    if (!interaction.boxId || !interaction.original) return;
    const [x1, y1, x2, y2] = interaction.original;
    const dx = current[0] - interaction.start[0];
    const dy = current[1] - interaction.start[1];
    setBoxes((items) =>
      items.map((box) => {
        if (box.box_id !== interaction.boxId) return box;
        if (interaction.mode === "resize") {
          return {
            ...box,
            xyxy: [
              x1,
              y1,
              Math.min(1, Math.max(x1 + 0.002, current[0])),
              Math.min(1, Math.max(y1 + 0.002, current[1])),
            ],
          };
        }
        const width = x2 - x1;
        const height = y2 - y1;
        const nx1 = Math.min(1 - width, Math.max(0, x1 + dx));
        const ny1 = Math.min(1 - height, Math.max(0, y1 + dy));
        return { ...box, xyxy: [nx1, ny1, nx1 + width, ny1 + height] };
      }),
    );
  };

  const finish = () => {
    if (interaction?.mode === "draw" && drawing) {
      const [ax, ay, bx, by] = drawing;
      const xyxy: [number, number, number, number] = [
        Math.min(ax, bx),
        Math.min(ay, by),
        Math.max(ax, bx),
        Math.max(ay, by),
      ];
      if (xyxy[2] - xyxy[0] >= 0.01 && xyxy[3] - xyxy[1] >= 0.01) {
        const box: CaptureBox = {
          box_id: crypto.randomUUID(),
          class_id: activeClass,
          label: classes[activeClass],
          xyxy,
          confidence: null,
        };
        setBoxes((items) => [...items, box]);
        setSelectedBoxId(box.box_id);
      }
    }
    setDrawing(null);
    setInteraction(null);
  };

  const setClass = (classId: number) => {
    setActiveClass(classId);
    if (!selectedBoxId) return;
    setBoxes((items) =>
      items.map((box) =>
        box.box_id === selectedBoxId ? { ...box, class_id: classId, label: classes[classId] } : box,
      ),
    );
  };

  const save = async (reviewed: boolean, empty = false) => {
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await api.reviewCapture(frame.frame_id, {
        boxes: empty ? [] : boxes,
        review_status: reviewed ? "reviewed" : "draft",
      });
      setBoxes(updated.boxes);
      setSelectedBoxId(updated.boxes[0]?.box_id ?? null);
      await onSaved(updated);
    } catch (error) {
      setSaveError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_260px]">
      <Card className="overflow-hidden">
        <div className="border-line flex flex-wrap items-center gap-2 border-b px-4 py-2.5">
          <Badge tone={frame.review_status === "reviewed" ? "good" : "warn"}>
            {frame.review_status}
          </Badge>
          <span className="text-txt-3 text-[11px]">{fmtDateTime(frame.captured_at)}</span>
          <span className="text-txt-3 font-mono text-[11px]">{frame.source_worker}</span>
          <span className="text-txt-3 font-mono text-[11px]">{frame.zone ?? "no zone"}</span>
          <span className="text-txt-3 ml-auto text-[11px]">
            Draw empty space · drag boxes · resize from the corner
          </span>
        </div>
        <div className="bg-[#07101f] p-3 sm:p-5">
          <div
            ref={overlayRef}
            onPointerDown={startDraw}
            onPointerMove={move}
            onPointerUp={finish}
            onPointerCancel={finish}
            className="relative mx-auto w-full max-w-5xl touch-none overflow-hidden rounded-sm shadow-2xl select-none"
            style={{ aspectRatio: `${frame.width} / ${frame.height}` }}
          >
            <CaptureImage
              frameId={frame.frame_id}
              alt="Captured worker phone frame"
              className="absolute inset-0 h-full w-full object-fill"
            />
            <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(rgba(96,165,250,.08)_1px,transparent_1px),linear-gradient(90deg,rgba(96,165,250,.08)_1px,transparent_1px)] bg-[size:10%_10%]" />
            {boxes.map((box) => (
              <BoxOverlay
                key={box.box_id}
                box={box}
                selected={box.box_id === selectedBoxId}
                onStart={startBoxInteraction}
              />
            ))}
            {drawing ? (
              <DraftOverlay xyxy={drawing} classId={activeClass} label={classes[activeClass]} />
            ) : null}
          </div>
        </div>
        <div className="border-line flex flex-wrap items-center gap-2 border-t px-4 py-3">
          <span className={`mr-auto text-[11px] ${saveError ? "text-red-500" : "text-txt-3"}`}>
            {saveError ?? `${boxes.length} boxes${dirty ? " · unsaved changes" : ""}`}
          </span>
          <Button
            tone="secondary"
            size="sm"
            onClick={() => {
              setBoxes((items) => items.filter((box) => box.box_id !== selectedBoxId));
              setSelectedBoxId(null);
            }}
            disabled={!selectedBoxId || saving}
          >
            Delete selected
          </Button>
          <Button tone="secondary" size="sm" onClick={() => void save(false)} disabled={saving}>
            Save draft
          </Button>
          <Button
            tone="secondary"
            size="sm"
            onClick={() => void save(true, true)}
            disabled={saving}
          >
            Confirm empty
          </Button>
          <Button tone="good" size="sm" onClick={() => void save(true)} disabled={saving}>
            {saving ? "Saving…" : "Save & mark reviewed"}
          </Button>
        </div>
      </Card>

      <aside>
        <SectionTitle>Label palette</SectionTitle>
        <Card className="overflow-hidden p-2">
          {classes.map((name, classId) => {
            const count = boxes.filter((box) => box.class_id === classId).length;
            return (
              <button
                key={name}
                type="button"
                onClick={() => setClass(classId)}
                className={`mb-1 flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-[12px] last:mb-0 ${
                  activeClass === classId
                    ? "border-accent bg-accent/10 text-txt"
                    : "text-txt-2 hover:bg-panel-2 border-transparent"
                }`}
              >
                <span
                  className="h-3 w-3 rounded-sm"
                  style={{ backgroundColor: BOX_COLORS[classId] }}
                />
                <span className="min-w-0 flex-1 truncate font-medium">{name}</span>
                {count ? <span className="text-txt-3 font-mono text-[10px]">{count}</span> : null}
              </button>
            );
          })}
        </Card>
        <p className="text-txt-3 mt-3 text-[11px] leading-relaxed">
          Model boxes are suggestions only. Check every worker and required PPE item before review.
        </p>
      </aside>
    </div>
  );
}

function BoxOverlay({
  box,
  selected,
  onStart,
}: {
  box: CaptureBox;
  selected: boolean;
  onStart: (event: ReactPointerEvent, box: CaptureBox, mode: "move" | "resize") => void;
}) {
  const [x1, y1, x2, y2] = box.xyxy;
  const color = BOX_COLORS[box.class_id];
  return (
    <div
      onPointerDown={(event) => onStart(event, box, "move")}
      className={`absolute cursor-move border-2 ${selected ? "z-20 shadow-[0_0_0_2px_rgba(255,255,255,.75)]" : "z-10"}`}
      style={{
        left: `${x1 * 100}%`,
        top: `${y1 * 100}%`,
        width: `${(x2 - x1) * 100}%`,
        height: `${(y2 - y1) * 100}%`,
        borderColor: color,
      }}
    >
      <span
        className="absolute -top-6 left-[-2px] max-w-40 truncate rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-bold text-slate-950"
        style={{ backgroundColor: color }}
      >
        {box.label}
        {box.confidence === null ? "" : ` ${Math.round(box.confidence * 100)}%`}
      </span>
      {selected ? (
        <button
          type="button"
          aria-label={`Resize ${box.label} bounding box`}
          onPointerDown={(event) => onStart(event, box, "resize")}
          className="absolute -right-2 -bottom-2 h-4 w-4 cursor-nwse-resize rounded-sm border-2 border-white"
          style={{ backgroundColor: color }}
        />
      ) : null}
    </div>
  );
}

function DraftOverlay({
  xyxy,
  classId,
  label,
}: {
  xyxy: [number, number, number, number];
  classId: number;
  label: string;
}) {
  const [ax, ay, bx, by] = xyxy;
  const x1 = Math.min(ax, bx);
  const y1 = Math.min(ay, by);
  const x2 = Math.max(ax, bx);
  const y2 = Math.max(ay, by);
  return (
    <div
      className="pointer-events-none absolute z-30 border-2 border-dashed"
      style={{
        left: `${x1 * 100}%`,
        top: `${y1 * 100}%`,
        width: `${(x2 - x1) * 100}%`,
        height: `${(y2 - y1) * 100}%`,
        borderColor: BOX_COLORS[classId],
      }}
    >
      <span
        className="absolute -top-6 left-[-2px] px-1.5 py-0.5 font-mono text-[10px] font-bold text-slate-950"
        style={{ backgroundColor: BOX_COLORS[classId] }}
      >
        {label}
      </span>
    </div>
  );
}
