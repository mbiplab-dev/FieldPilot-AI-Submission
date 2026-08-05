"use client";

import { useSyncExternalStore } from "react";
import { readVoice, serverVoice, subscribeVoice, type VoiceState } from "./speech";

/**
 * The voice-alert preference, surfaced into React.
 *
 * Mirrors `useSession` — `useSyncExternalStore` is how a browser-only value (here `localStorage`
 * plus Web Speech API support) reaches a component without reading it inside an effect.
 */
export function useVoice(): VoiceState {
  return useSyncExternalStore(subscribeVoice, readVoice, serverVoice);
}
