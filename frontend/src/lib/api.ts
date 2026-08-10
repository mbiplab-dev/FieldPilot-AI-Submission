/**
 * Typed client for the FieldPilot backend REST API.
 * All calls go through the same-origin `/api` rewrite → http://localhost:8100.
 */

import { clearSession, sessionToken } from "./session";

const BASE = "/api";

/** Thrown by every call in this module. `status === 0` means the network never answered. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

/** FastAPI puts human-readable failures in `detail` (string) or validation arrays. */
function detailOf(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const first = detail[0];
    if (first && typeof first === "object" && typeof (first as { msg?: unknown }).msg === "string") {
      return (first as { msg: string }).msg;
    }
  }
  return null;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // Every role-gated endpoint needs this; unauthenticated calls (e.g. /auth/login itself)
  // simply have no token to attach.
  const headers = new Headers(init?.headers);
  const token = sessionToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { cache: "no-store", ...init, headers });
  } catch {
    throw new ApiError(0, "Backend unreachable — is the API running on :8100?");
  }
  const text = await res.text();
  const body = text ? parseJson(text) : null;
  if (!res.ok) {
    // A 401 means the token is gone or expired server-side — drop it locally too, so every
    // page's session hook sees "signed out" instead of retrying with a dead token forever.
    if (res.status === 401) clearSession();
    throw new ApiError(res.status, detailOf(body) ?? `${res.status} ${res.statusText || "request failed"}`);
  }
  return body as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

/**
 * The edge server (vision pipeline) sits behind a different rewrite from the backend REST API —
 * see `next.config.ts`. Worker camera feeds are served there because that is the process holding
 * the frames, so they cannot go through {@link req}'s `/api` base.
 */
const EDGE_BASE = "/feed";

async function edgeGet<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${EDGE_BASE}${path}`, { cache: "no-store" });
  } catch {
    throw new ApiError(0, "Edge server unreachable — is the vision service running on :8000?");
  }
  const text = await res.text();
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText || "request failed"}`);
  return (text ? parseJson(text) : null) as T;
}

const get = <T,>(path: string) => req<T>(path);
const post = <T,>(path: string, body?: unknown) => req<T>(path, jsonInit("POST", body));
const put = <T,>(path: string, body?: unknown) => req<T>(path, jsonInit("PUT", body));
const del = <T,>(path: string) => req<T>(path, jsonInit("DELETE"));

type QueryValue = string | number | boolean | undefined | null;

/** Builds `?a=1&b=2`, dropping empty/undefined values. Returns "" when nothing is set. */
function qs(params: Record<string, QueryValue>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

/* ----------------------------------- auth ----------------------------------- */

export type UserRole = "worker" | "site_manager";

export interface AuthUser {
  user_id: string;
  username: string;
  display_name: string;
  role: UserRole;
  worker_id: string | null;
  active?: boolean;
}

export interface LoginInput {
  username: string;
  password: string;
}

export interface LoginResult {
  token: string;
  user: AuthUser;
}

export interface CreateUserInput {
  username: string;
  password: string;
  role: UserRole;
  display_name?: string;
  worker_id?: string;
}

/* ------------------------------- types ------------------------------- */

export type Severity = "low" | "medium" | "high" | "critical";
export type AlertState = "NEW" | "ACTIVE" | "RESOLVED" | "SUPPRESSED";

export interface Alert {
  alert_id: string;
  dedup_key: string;
  event_type: string;
  worker_id: string | null;
  camera_id: string | null;
  zone: string | null;
  severity: Severity;
  state: AlertState;
  hit_count: number;
  confidence: number;
  first_seen: number;
  last_seen: number;
  resolved_at: number | null;
  suppressed_at: number | null;
  message: string | null;
  payload: Record<string, unknown>;
  image_url: string | null;
  video_url: string | null;
}

export interface WorkerSummary {
  worker_id: string;
  zone: string | null;
  active_alerts: number;
}

export interface WorkerTimeline {
  worker_id: string;
  current_zone: string | null;
  live_status: "ok" | "flagged" | "at_risk";
  safety_score: number;
  active_alerts: Alert[];
  past_alerts: Alert[];
  recent_events: PlatformEvent[];
}

export interface PlatformEvent {
  event_id: string;
  event_type: string;
  worker_id: string | null;
  camera_id: string;
  zone: string | null;
  timestamp: number;
  confidence: number;
  severity: Severity;
  payload: Record<string, unknown>;
}

export interface Rule {
  rule_id: string;
  name: string;
  enabled: boolean;
  priority: number;
  event_types: string[];
  conditions: { field: string; op: string; value?: unknown }[];
  action: Record<string, unknown> & { type?: string };
  cooldown_s: number;
}

export interface NotificationItem {
  notification_id: string;
  channel: string;
  subject: string | null;
  body: string | null;
  status: "queued" | "sent" | "failed" | "skipped";
  attempts: number;
  created_at: number;
}

/* --------------------------------- zones --------------------------------- */

export type HazardLevel = "low" | "medium" | "high";

export interface Zone {
  zone_id: string;
  name: string;
  project_id: string | null;
  hazard_level: HazardLevel;
  danger: boolean;
  active: boolean;
  description: string | null;
  created_at: number;
  updated_at: number;
}

export interface ZoneCreate {
  name: string;
  project_id?: string;
  hazard_level?: HazardLevel;
  danger?: boolean;
  active?: boolean;
  description?: string;
}

export type ZoneUpdate = Partial<ZoneCreate>;

/** One worker currently checked into a zone. */
export interface ZoneOccupant {
  worker_id: string;
  display_name?: string | null;
  entered_at: number;
}

export interface ZoneWarningCounts {
  total: number;
  today: number;
  outstanding: number;
  by_severity: Record<string, number>;
}

/**
 * `GET /zones/occupancy` — who is where, and which zones are generating the most warnings.
 * Rows come back worst-first; `risk_rank` is 1-based over that order.
 */
export interface ZoneOccupancy {
  zone_id: string;
  name: string;
  hazard_level: HazardLevel;
  danger: boolean;
  workers: ZoneOccupant[];
  worker_count: number;
  warnings: ZoneWarningCounts;
  risk_score: number;
  risk_rank: number;
}

/* ---------------------------- worker questions ---------------------------- */

export type QuestionStatus = "pending" | "answered" | "closed";

export interface QuestionCitation {
  citation: string;
  clause?: string | null;
  source?: string | null;
  page?: number | null;
  zone?: string | null;
  score?: number | null;
  text?: string;
}

export interface WorkerQuestion {
  question_id: string;
  worker_id: string;
  zone: string | null;
  text: string;
  image_url: string | null;
  status: QuestionStatus;
  llm_answer: string | null;
  llm_grounded: boolean | null;
  llm_model: string | null;
  citations: QuestionCitation[];
  manager_reply: string | null;
  manager_id: string | null;
  replied_at: number | null;
  created_at: number;
  answered_at: number | null;
}

export interface QuestionReplyInput {
  reply: string;
}

export interface QuestionStats {
  total: number;
  pending: number;
  answered: number;
  closed: number;
  awaiting_manager: number;
}

/* ---------------------------- supervisor feedback ---------------------------- */

export type FeedbackDecision = "approve" | "reject";

export interface FeedbackInput {
  decision: FeedbackDecision;
  label?: string;
  notes?: string;
  reviewer?: string;
  bbox?: number[];
}

export interface Feedback {
  feedback_id: string;
  alert_id: string;
  event_type: string;
  decision: FeedbackDecision;
  label: string | null;
  image_path: string | null;
  zone: string | null;
  worker_id: string | null;
  reviewer: string | null;
  notes: string | null;
  confidence: number | null;
  consumed_at: number | null;
  consumed_by: string | null;
  created_at: number;
  bbox?: number[];
}

export interface FeedbackStats {
  approved: number;
  rejected: number;
  total: number;
  unconsumed: number;
  approval_rate: number | null;
}

/* ------------------------------- learning loop ------------------------------- */

export type LearningStatus = "pending" | "running" | "completed" | "failed" | "blocked";

export interface LearningRun {
  run_id: string;
  status: LearningStatus;
  base_weights: string;
  weights_path: string | null;
  dataset_dir: string;
  samples: number;
  epochs: number;
  map50_before: number | null;
  map50_after: number | null;
  delta: number | null;
  promoted: boolean;
  message: string;
  created_at: number;
  finished_at: number | null;
}

export interface TrainRequest {
  epochs?: number;
  base_weights?: string;
}

/* ----------------------------------- RFIs ----------------------------------- */

export type RFIStatus = "pending_review" | "approved" | "rejected";

export interface RFICitation {
  citation: string;
  clause: string | null;
  source: string | null;
  page: number | null;
  zone: string | null;
  score: number;
  text: string;
}

export interface RFIPayload {
  project_id?: string | null;
  grounded?: boolean;
  citations?: RFICitation[];
  llm_used?: boolean;
}

export interface RFI {
  rfi_id: string;
  event_id: string | null;
  title: string | null;
  summary: string | null;
  body: string | null;
  priority: string | null;
  zone: string | null;
  status: RFIStatus;
  citation: string | null;
  created_at: number;
  reviewed_at: number | null;
  reviewer: string | null;
  payload: RFIPayload;
}

export interface RFIReview {
  reviewer?: string;
  notes?: string;
}

/* ------------------------------ blueprints / RAG ------------------------------ */

export interface EmbeddingInfo {
  backend: string;
  model: string | null;
  semantic: boolean;
}

export interface BlueprintDocument {
  name: string;
  project_id: string | null;
  zone: string | null;
  category: string | null;
  size_bytes: number;
}

export interface BlueprintIndex {
  documents: BlueprintDocument[];
  indexed_chunks: number;
  embeddings: EmbeddingInfo;
  available: boolean;
}

export interface BlueprintIngestResult {
  files: number;
  chunks: number;
  upserted: number;
  skipped: string[];
  degraded_embeddings: boolean;
}

export interface BlueprintSearchRequest {
  query: string;
  project_id?: string;
  zone?: string;
  category?: string;
  top_k?: number;
}

export interface BlueprintChunk {
  chunk_id: string;
  text: string;
  project_id: string | null;
  zone: string | null;
  category: string | null;
  source: string | null;
  page: number | null;
  clause: string | null;
  score: number;
}

/* --------------------------------- inspections --------------------------------- */

export interface Inspection {
  inspection_id: string;
  priority: string | null;
  zone: string | null;
  message: string | null;
  status: string;
  notes: string | null;
  created_at: number;
}

/* ------------------------------------ health ------------------------------------ */

export interface BroadcastHealth {
  connected: number;
  devices: number;
  dashboards: number;
  by_zone: Record<string, number>;
  delivered: number;
  dropped: number;
}

export interface RagHealth {
  available: boolean;
  indexed_chunks: number;
  embeddings: EmbeddingInfo;
}

export interface PpeHealth {
  enabled: boolean;
  model: string | null;
  reason: string | null;
}

/** Compact learning summary embedded in `/health` — a subset of {@link LearningRun}. */
export interface HealthLearning {
  run_id?: string;
  status?: LearningStatus;
  map50_before?: number | null;
  map50_after?: number | null;
  delta?: number | null;
  promoted?: boolean;
  message?: string;
  created_at?: number;
  finished_at?: number | null;
}

/**
 * Fields beyond `status`/`tracked_alerts`/`rules` are optional on purpose: the
 * dashboard has to stay readable against an older backend that has not shipped
 * the learning/broadcast/RAG sections yet.
 */
export interface Health {
  status: string;
  tracked_alerts: number;
  rules: number;
  zones?: number;
  feedback?: number;
  rfis_pending?: number;
  learning?: HealthLearning | null;
  broadcast?: BroadcastHealth;
  rag?: RagHealth;
  ppe?: PpeHealth;
}

/* ------------------------------- calls ------------------------------- */

export const api = {
  /* auth */
  login: (input: LoginInput) => post<LoginResult>("/auth/login", input),
  logout: () => post<{ ok: boolean }>("/auth/logout"),
  me: () => get<AuthUser>("/auth/me"),
  users: () => get<{ users: AuthUser[] }>("/auth/users"),
  createUser: (input: CreateUserInput) => post<AuthUser>("/auth/users", input),

  health: () => get<Health>("/health"),

  alerts: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    q.set("limit", params.limit ?? "300");
    return get<{ alerts: Alert[] }>(`/alerts?${q}`);
  },
  alert: (id: string) => get<Alert>(`/alerts/${encodeURIComponent(id)}`),
  alertAction: (id: string, op: "resolve" | "suppress" | "unsuppress") =>
    post<unknown>(`/alerts/${encodeURIComponent(id)}/${op}`),

  workers: () => get<{ workers: WorkerSummary[] }>("/workers"),
  workerTimeline: (id: string) =>
    get<WorkerTimeline>(`/workers/${encodeURIComponent(id)}/timeline`),

  rules: () => get<{ rules: Rule[] }>("/rules"),
  updateRule: (id: string, rule: Omit<Rule, "rule_id">) =>
    put<Rule>(`/rules/${encodeURIComponent(id)}`, rule),

  notifications: () => get<{ notifications: NotificationItem[] }>("/notifications?limit=40"),
  eventStats: () => get<{ counts_by_type: Record<string, number> }>("/events/stats"),

  /* zones */
  zones: () => get<{ zones: Zone[] }>("/zones"),
  zone: (id: string) => get<Zone>(`/zones/${encodeURIComponent(id)}`),
  createZone: (zone: ZoneCreate) => post<Zone>("/zones", zone),
  updateZone: (id: string, zone: ZoneUpdate) => put<Zone>(`/zones/${encodeURIComponent(id)}`, zone),
  deleteZone: (id: string) => del<{ deleted: boolean }>(`/zones/${encodeURIComponent(id)}`),
  zoneOccupancy: () => get<{ zones: ZoneOccupancy[] }>("/zones/occupancy"),

  /* worker questions — the manager-facing inbox */
  questions: (params: { status?: QuestionStatus | ""; zone?: string; limit?: number } = {}) =>
    get<{ questions: WorkerQuestion[] }>(
      `/questions${qs({ status: params.status, zone: params.zone, limit: params.limit ?? 100 })}`,
    ),
  question: (id: string) => get<WorkerQuestion>(`/questions/${encodeURIComponent(id)}`),
  replyToQuestion: (id: string, input: QuestionReplyInput) =>
    post<WorkerQuestion>(`/questions/${encodeURIComponent(id)}/reply`, input),
  questionStats: () => get<QuestionStats>("/questions/stats"),

  /* supervisor feedback — closes the learning loop */
  submitFeedback: (alertId: string, input: FeedbackInput) =>
    post<Feedback>(`/alerts/${encodeURIComponent(alertId)}/feedback`, input),
  feedback: (params: { decision?: FeedbackDecision; event_type?: string; limit?: number } = {}) =>
    get<{ feedback: Feedback[] }>(`/feedback${qs({ ...params, limit: params.limit ?? 500 })}`),
  feedbackStats: () => get<FeedbackStats>("/feedback/stats"),

  /* learning loop */
  train: (input: TrainRequest = {}) => post<LearningRun>("/learning/train", input),
  learningRuns: (limit = 25) => get<{ runs: LearningRun[] }>(`/learning/runs${qs({ limit })}`),
  learningRun: (id: string) => get<LearningRun>(`/learning/runs/${encodeURIComponent(id)}`),
  latestLearningRun: () => get<LearningRun | null>("/learning/latest"),

  /* RFI review queue */
  rfis: (params: { status?: RFIStatus | ""; limit?: number } = {}) =>
    get<{ rfis: RFI[] }>(`/rfis${qs({ status: params.status, limit: params.limit ?? 50 })}`),
  rfi: (id: string) => get<RFI>(`/rfis/${encodeURIComponent(id)}`),
  reviewRfi: (id: string, decision: "approve" | "reject", review: RFIReview = {}) =>
    post<RFI>(`/rfis/${encodeURIComponent(id)}/${decision}`, review),

  /* blueprints / RAG */
  blueprints: () => get<BlueprintIndex>("/blueprints"),
  ingestBlueprints: (replace = false) =>
    post<BlueprintIngestResult>("/blueprints/ingest", { replace }),
  searchBlueprints: (input: BlueprintSearchRequest) =>
    post<{ chunks: BlueprintChunk[] }>("/blueprints/search", input),

  /* inspections */
  inspections: (limit = 20) => get<{ inspections: Inspection[] }>(`/inspections${qs({ limit })}`),
  completeInspection: (id: string, notes?: string) =>
    post<Inspection>(`/inspections/${encodeURIComponent(id)}/complete`, { notes }),

  inspectionMode: () => get<{ enabled: boolean }>("/control/inspection"),
  setInspectionMode: (enabled: boolean) =>
    post<{ enabled: boolean }>("/control/inspection", { enabled }),

  /* worker phone cameras — served by the edge, not the backend, so this bypasses `/api` */
  workerFeeds: () => edgeGet<{ feeds: WorkerCameraFeed[]; stats: WorkerFeedStats }>("/workers/live"),

  /* direct messages */
  messageThreads: () => get<{ threads: MessageThread[] }>("/messages/threads"),
  unreadMessages: () => get<{ unread: number }>("/messages/unread"),
  thread: (workerId: string) =>
    get<{ worker_id: string; messages: DirectMessage[] }>(
      `/messages/${encodeURIComponent(workerId)}`,
    ),
  markThreadRead: (workerId: string) =>
    post<{ marked_read: number }>(`/messages/${encodeURIComponent(workerId)}/read`),
  /** Text and/or a recorded voice note. Multipart because the audio is a file. */
  sendMessage: (workerId: string, input: { text?: string; audio?: Blob }) => {
    const form = new FormData();
    form.append("text", input.text ?? "");
    if (input.audio) form.append("audio", input.audio, "voice.webm");
    return req<DirectMessage>(`/messages/${encodeURIComponent(workerId)}`, {
      method: "POST",
      body: form,
    });
  },
};

export interface DirectMessage {
  message_id: string;
  worker_id: string;
  sender_role: "worker" | "site_manager";
  sender_id: string;
  sender_name: string;
  text: string;
  audio_url: string | null;
  audio_seconds: number | null;
  read_at: number | null;
  created_at: number;
}

export interface MessageThread {
  worker_id: string;
  worker_name: string | null;
  messages: number;
  unread: number;
  last_at: number;
  last_text: string;
  last_sender_role: "worker" | "site_manager" | null;
  last_has_audio: boolean;
}

/** One worker's phone camera, as reported by the edge server. */
export interface WorkerCameraFeed {
  worker_id: string;
  zone: string | null;
  display_name: string | null;
  started_at: number;
  last_frame_at: number | null;
  /** Seconds since the last frame, or null if none has arrived. */
  age_s: number | null;
  /** False once the phone stops sending — backgrounded, out of signal, or battery saver. */
  live: boolean;
  frames: number;
  hazards: number;
  fps: number;
  width: number;
  height: number;
}

export interface WorkerFeedStats {
  streaming: number;
  known: number;
  workers: string[];
}

/** MJPEG URL for one worker's phone camera, annotated with what the server detected. */
export function workerStreamUrl(workerId: string): string {
  return `${EDGE_BASE}/workers/${encodeURIComponent(workerId)}/stream`;
}

/* ------------------------------ helpers ------------------------------ */

export function timeAgo(ts?: number | null): string {
  if (!ts) return "–";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fmtTime(ts?: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "–";
}

export function fmtDateTime(ts?: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleString() : "–";
}

/** mAP50 and friends live in 0…1 — render 4 decimals so small deltas stay visible. */
export function fmtMetric(v?: number | null): string {
  return v === null || v === undefined ? "–" : v.toFixed(4);
}

/** Always signed, so a regression can never be mistaken for an improvement. */
export function fmtDelta(v?: number | null): string {
  if (v === null || v === undefined) return "–";
  return `${v > 0 ? "+" : v < 0 ? "" : "±"}${v.toFixed(4)}`;
}

export function fmtPercent(v?: number | null): string {
  return v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`;
}

export function fmtBytes(n?: number | null): string {
  if (n === null || n === undefined) return "–";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Turns anything thrown by a call into something safe to render. */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return "Unexpected error";
}
