/**
 * The signed-in session, held outside React so that non-React code (the API
 * client) can read the bearer token synchronously.
 *
 * Why an external store rather than `useState` + an effect:
 *   * `localStorage` does not exist while the page is server-rendered, and the
 *     eslint rule `react-hooks/set-state-in-effect` forbids reading it in an
 *     effect and pushing it into state. `useSyncExternalStore` is the supported
 *     way to surface a browser-only value.
 *   * `hydrated` is part of the snapshot on purpose. The server snapshot has
 *     `hydrated: false`, so route guards can tell "not signed in" apart from
 *     "we have not looked yet" and never bounce a signed-in user to /login on
 *     the hydration pass.
 *
 * This is demo-grade persistence: the token lives in `localStorage`, which is
 * readable by any script on the origin. Good enough for a single-site demo,
 * not a pattern to copy into a production tenant.
 */

import type { AuthUser } from "./api";

const STORAGE_KEY = "fp-session";

export interface Session {
  /** False until the browser's stored session has been read (i.e. during SSR/hydration). */
  hydrated: boolean;
  token: string | null;
  user: AuthUser | null;
}

/** Returned while rendering on the server and during the hydration pass. */
const SERVER_SESSION: Session = Object.freeze({ hydrated: false, token: null, user: null });
/** Read the storage, found nothing (or something unusable). */
const SIGNED_OUT: Session = Object.freeze({ hydrated: true, token: null, user: null });

/**
 * The current snapshot. `useSyncExternalStore` compares snapshots by identity,
 * so this object is replaced only when something actually changed — never
 * rebuilt per read.
 */
let cache: Session = SERVER_SESSION;
let readDone = false;
let storageBound = false;

const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

/** Accepts anything and returns a user only when the required fields are really there. */
function normaliseUser(value: unknown): AuthUser | null {
  if (!value || typeof value !== "object") return null;
  const rec = value as Record<string, unknown>;
  const role = rec.role;
  if (role !== "worker" && role !== "site_manager") return null;
  const username = typeof rec.username === "string" && rec.username ? rec.username : null;
  if (!username) return null;
  const displayName = typeof rec.display_name === "string" && rec.display_name ? rec.display_name : null;
  return {
    user_id: typeof rec.user_id === "string" && rec.user_id ? rec.user_id : username,
    username,
    display_name: displayName ?? username,
    role,
    worker_id: typeof rec.worker_id === "string" && rec.worker_id ? rec.worker_id : null,
  };
}

function fromStorage(): Session {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return SIGNED_OUT;
  }
  if (!raw) return SIGNED_OUT;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return SIGNED_OUT;
  }
  const rec = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  const token = rec && typeof rec.token === "string" && rec.token ? rec.token : null;
  const user = normaliseUser(rec?.user);
  if (!token || !user) return SIGNED_OUT;
  return Object.freeze({ hydrated: true, token, user });
}

/** Keeps two tabs of the demo in step — signing out in one signs out the other. */
function bindStorageSync(): void {
  if (storageBound || typeof window === "undefined") return;
  storageBound = true;
  window.addEventListener("storage", (event) => {
    if (event.key !== null && event.key !== STORAGE_KEY) return;
    commit(fromStorage());
  });
}

function sameUser(a: AuthUser | null, b: AuthUser | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.user_id === b.user_id &&
    a.username === b.username &&
    a.display_name === b.display_name &&
    a.role === b.role &&
    a.worker_id === b.worker_id
  );
}

function commit(next: Session): void {
  if (cache.hydrated === next.hydrated && cache.token === next.token && sameUser(cache.user, next.user)) {
    return;
  }
  cache = next;
  emit();
}

/** `getSnapshot` for {@link useSyncExternalStore} — also performs the one-time read. */
export function readSession(): Session {
  if (!readDone && typeof window !== "undefined") {
    readDone = true;
    cache = fromStorage();
    bindStorageSync();
  }
  return cache;
}

/** `getServerSnapshot` for {@link useSyncExternalStore}. */
export function serverSession(): Session {
  return SERVER_SESSION;
}

export function subscribeSession(onChange: () => void): () => void {
  bindStorageSync();
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

/** Synchronous token accessor for the API client. */
export function sessionToken(): string | null {
  return readSession().token;
}

export function setSession(token: string, user: unknown): boolean {
  readSession(); // never let the lazy first read clobber a session we just stored
  const clean = normaliseUser(user);
  if (!token || !clean) return false;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ token, user: clean }));
  } catch {
    // Private mode / storage full: the session still works until this tab closes.
  }
  commit(Object.freeze({ hydrated: true, token, user: clean }));
  return true;
}

/** Refreshes the cached profile (e.g. after `GET /auth/me`) without touching the token. */
export function updateSessionUser(user: unknown): void {
  const current = readSession();
  if (!current.token) return;
  setSession(current.token, user);
}

export function clearSession(): void {
  readSession();
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing to do — the in-memory snapshot below is what the UI reads.
  }
  commit(SIGNED_OUT);
}
