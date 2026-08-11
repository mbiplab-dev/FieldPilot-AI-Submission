/**
 * Spoken alerts in the browser, via the Web Speech API.
 *
 * The backend sends the *sentence* on each broadcast frame (`data.speech`, built by
 * `fieldpilot/alerts/speech.py`) and the browser synthesises it locally. Speech therefore needs no
 * server audio stack, no API key, and no round trip — which matters, because the backend's own
 * `alerts/tts.py` renders onto the *server's* speakers, a machine nobody is listening to.
 *
 * Four browser realities this module exists to handle honestly:
 *
 *   1. **Autoplay policy.** Chrome silently discards `speak()` until the user has interacted with
 *      the page. A toggle that claimed "voice on" while the browser dropped every utterance would
 *      be a lie, so enabling voice requires a real click and primes the engine inside that gesture.
 *   2. **Voices load asynchronously.** `getVoices()` returns `[]` until `voiceschanged` fires, so
 *      voice selection is resolved lazily per utterance rather than cached at import.
 *   3. **The same alert arrives more than once** — over the socket and again on the next poll — so
 *      announcements are deduplicated by key.
 *   4. **Alert storms.** `speechSynthesis` queues indefinitely; twelve queued sentences would still
 *      be talking long after they mattered. See {@link announce} for the priority policy.
 *
 * The preference is held outside React (same reasoning as `session.ts`): `localStorage` does not
 * exist during SSR, and `react-hooks/set-state-in-effect` forbids reading it in an effect and
 * pushing it into state. `useSyncExternalStore` is the supported way to surface a browser value.
 */

const STORAGE_KEY = "fp-voice";

/** Severities that interrupt whatever is currently being spoken. */
const INTERRUPTING = new Set(["critical", "high"]);

/** Cap on remembered announcement keys, so a long shift cannot grow this without bound. */
const SEEN_LIMIT = 300;

export interface VoiceState {
  /** False until the browser's stored preference has been read (i.e. during SSR/hydration). */
  hydrated: boolean;
  /** The user's preference. */
  enabled: boolean;
  /** Whether this browser can synthesise speech at all. */
  supported: boolean;
}

const SERVER_STATE: VoiceState = Object.freeze({
  hydrated: false,
  enabled: false,
  supported: false,
});

let cache: VoiceState = SERVER_STATE;
let readDone = false;
const listeners = new Set<() => void>();

/** Announcement keys already spoken — the socket and the poll both deliver the same alert. */
const spoken = new Set<string>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

function synth(): SpeechSynthesis | null {
  if (typeof window === "undefined") return null;
  return "speechSynthesis" in window ? window.speechSynthesis : null;
}

/** Not exported — every consumer reads `supported` off {@link VoiceState} instead. */
function speechSupported(): boolean {
  return synth() !== null;
}

function fromStorage(): VoiceState {
  const supported = speechSupported();
  let stored: string | null = null;
  try {
    stored = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private mode: fall through to the default.
  }
  return Object.freeze({
    hydrated: true,
    // Voice is opt-in. A dashboard that starts talking the moment it loads would be hostile in a
    // shared office, and the browser would drop the first utterance anyway (reality 1).
    enabled: supported && stored === "on",
    supported,
  });
}

function commit(next: VoiceState): void {
  if (
    cache.hydrated === next.hydrated &&
    cache.enabled === next.enabled &&
    cache.supported === next.supported
  ) {
    return;
  }
  cache = next;
  emit();
}

/** `getSnapshot` for `useSyncExternalStore` — also performs the one-time read. */
export function readVoice(): VoiceState {
  if (!readDone && typeof window !== "undefined") {
    readDone = true;
    cache = fromStorage();
  }
  return cache;
}

/** `getServerSnapshot` for `useSyncExternalStore`. */
export function serverVoice(): VoiceState {
  return SERVER_STATE;
}

export function subscribeVoice(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

/** Picks an English voice if one is installed, else lets the platform choose. */
function pickVoice(engine: SpeechSynthesis): SpeechSynthesisVoice | null {
  const voices = engine.getVoices();
  if (voices.length === 0) return null; // not loaded yet — the platform default still speaks
  const preferred =
    voices.find((v) => v.lang.replace("_", "-").toLowerCase().startsWith("en-")) ??
    voices.find((v) => v.lang.toLowerCase().startsWith("en"));
  return preferred ?? null;
}

/**
 * Turn voice on or off. **Must be called from a real user gesture** when enabling: the priming
 * utterance below is what satisfies the autoplay policy, and it only counts inside a click.
 */
export function setVoiceEnabled(enabled: boolean): void {
  readVoice();
  const engine = synth();
  if (!engine) return;

  try {
    window.localStorage.setItem(STORAGE_KEY, enabled ? "on" : "off");
  } catch {
    // Preference will not survive a reload; voice still works for this session.
  }

  if (enabled) {
    // Speaking *within* the click unlocks the engine for later, socket-driven utterances.
    engine.cancel();
    engine.speak(new SpeechSynthesisUtterance("Voice alerts on."));
  } else {
    engine.cancel();
  }
  commit(Object.freeze({ hydrated: true, enabled, supported: true }));
}

export function stopSpeaking(): void {
  synth()?.cancel();
}

/** Speak now, bypassing preference and dedup checks. Used for the toggle's confirmation. */
function utter(engine: SpeechSynthesis, text: string, interrupt: boolean): void {
  if (interrupt) engine.cancel();
  const u = new SpeechSynthesisUtterance(text);
  const voice = pickVoice(engine);
  if (voice) u.voice = voice;
  u.rate = 1;
  u.pitch = 1;
  u.volume = 1;
  engine.speak(u);
}

/**
 * Announce an alert once.
 *
 * `key` deduplicates: the same alert delivered by socket and by poll speaks once. Returns what
 * happened, so callers (and tests) can tell a drop from a silent failure.
 *
 * Priority policy, because `speechSynthesis` will otherwise queue without limit:
 *   * critical / high — cancel whatever is talking and say this instead. This is the "stop work"
 *     case; a stale medium alert must not delay it.
 *   * medium / low — queued only while nothing is already waiting behind the current utterance.
 *     Otherwise the sentence is dropped: it is still on screen, and a dashboard reciting a
 *     minute-old backlog is worse than silence.
 */
export function announce(
  key: string,
  text: string,
  severity?: string | null,
): "spoken" | "duplicate" | "dropped" | "disabled" {
  const state = readVoice();
  if (!state.enabled || !state.supported) return "disabled";

  const sentence = (text ?? "").trim();
  if (!sentence) return "dropped";

  if (spoken.has(key)) return "duplicate";
  spoken.add(key);
  if (spoken.size > SEEN_LIMIT) {
    // Insertion-ordered, so the oldest keys go first.
    for (const old of [...spoken].slice(0, spoken.size - SEEN_LIMIT)) spoken.delete(old);
  }

  const engine = synth();
  if (!engine) return "disabled";

  const urgent = INTERRUPTING.has((severity ?? "").toLowerCase());
  // The API exposes queue state as two booleans and no depth, so the only honest backlog test is
  // "is something already waiting behind what is being spoken". If so, a non-urgent sentence is
  // dropped rather than deepening a queue that is already talking about the past.
  if (!urgent && engine.pending) return "dropped";

  utter(engine, sentence, urgent);
  return "spoken";
}

/** Test/diagnostic helper: speak a sample line so the user can confirm the voice works. */
export function speakSample(): void {
  const engine = synth();
  if (!engine) return;
  utter(engine, "Stop work. Rebar spacing is 40 millimetres above spec.", true);
}

/** Clears dedup memory — used when a different user signs in. */
export function resetAnnouncements(): void {
  spoken.clear();
}
