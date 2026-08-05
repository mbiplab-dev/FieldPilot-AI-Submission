"use client";

import { setVoiceEnabled, speakSample } from "@/lib/speech";
import { useVoice } from "@/lib/useVoice";

/**
 * The speaker button that arms spoken alerts.
 *
 * Enabling *must* happen inside this click: the priming utterance in `setVoiceEnabled` is what
 * satisfies the browser's autoplay policy, so later socket-driven alerts are allowed to speak.
 */
export function VoiceToggle() {
  const { enabled, supported, hydrated } = useVoice();

  // Render nothing until we know, so the button never flips state on the hydration pass.
  if (!hydrated) return <span className="h-8 w-8" aria-hidden />;

  if (!supported) {
    return (
      <span
        title="This browser cannot synthesise speech, so spoken alerts are unavailable."
        className="grid h-8 w-8 place-items-center rounded-lg border border-line-soft text-txt-3"
        aria-label="Spoken alerts unavailable in this browser"
      >
        <SpeakerOff />
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setVoiceEnabled(!enabled)}
      onDoubleClick={() => enabled && speakSample()}
      aria-pressed={enabled}
      title={
        enabled
          ? "Spoken alerts are on — double-click to hear a sample"
          : "Turn on spoken alerts (hazards are read aloud as they arrive)"
      }
      className={`grid h-8 w-8 place-items-center rounded-lg border transition-colors ${
        enabled
          ? "border-accent bg-accent/10 text-accent"
          : "border-line bg-panel-2 text-txt-2 hover:text-txt"
      }`}
    >
      {enabled ? <SpeakerOn /> : <SpeakerOff />}
    </button>
  );
}

function SpeakerOn() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 5 6 9H2v6h4l5 4V5z" />
      <path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14" />
    </svg>
  );
}

function SpeakerOff() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 5 6 9H2v6h4l5 4V5z" />
      <path d="m16 9 5 6m0-6-5 6" />
    </svg>
  );
}
