"use client";

import { useEffect } from "react";
import { announce, stopSpeaking } from "@/lib/speech";
import { useVoice } from "@/lib/useVoice";
import { useLiveFeed, type LiveFrame } from "@/lib/useLiveFeed";

/** Topics that carry a spoken sentence from the backend. */
const SPOKEN_TOPICS = ["alert", "advisory"] as const;

/**
 * Reads hazard alerts aloud as they arrive.
 *
 * Renders nothing. It is mounted once, above the page content, so an alert is still spoken while
 * the manager is on any page — a hazard that only speaks on `/alerts` would be a hazard nobody
 * heard. The socket is opened only while voice is on, so a muted dashboard costs nothing.
 *
 * The sentence itself is authored by the backend (`alerts/speech.py`) and arrives as `data.speech`,
 * already phrased for the dashboard audience. This component deliberately does not compose its own
 * wording: two independent phrasings of one alert would drift, and the backend is the only side
 * that knows the full payload.
 */
export function VoiceAnnouncer() {
  const { enabled } = useVoice();

  useLiveFeed({
    topics: SPOKEN_TOPICS,
    enabled,
    onFrame: (frame: LiveFrame) => {
      const rec = frame.data as Record<string, unknown> | null;
      if (!rec || typeof rec !== "object") return;

      const sentence = typeof rec.speech === "string" ? rec.speech : null;
      // No `speech` means an older backend, or a topic that carries none. Staying silent is
      // correct: inventing a sentence here is exactly the drift this component avoids.
      if (!sentence) return;

      const alertId = typeof rec.alert_id === "string" ? rec.alert_id : null;
      const severity = typeof rec.severity === "string" ? rec.severity : null;
      // Keyed by topic too: the primary alert and a later advisory about it are different
      // announcements, and the fallback keeps frames without an id from collapsing together.
      announce(`${frame.topic}:${alertId ?? `seq-${frame.seq}`}`, sentence, severity);
    },
  });

  // Muting, navigating away, or signing out must silence whatever is mid-sentence.
  useEffect(() => {
    if (!enabled) stopSpeaking();
    return stopSpeaking;
  }, [enabled]);

  return null;
}
