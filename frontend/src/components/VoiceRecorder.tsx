"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Hard stop, so a forgotten recording cannot run until the tab closes. */
const MAX_SECONDS = 120;

/**
 * Records a short voice message with the browser microphone.
 *
 * Press to start, press again to send. The microphone track is stopped explicitly on every exit
 * path — leaving it open would keep the browser's recording indicator lit and imply the dashboard
 * is still listening when it is not.
 */
export function VoiceRecorder({
  onRecorded,
  onError,
  disabled = false,
}: {
  onRecorded: (audio: Blob) => void;
  onError?: (message: string) => void;
  disabled?: boolean;
}) {
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const releaseMic = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (tickRef.current !== null) {
      clearInterval(tickRef.current);
      tickRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }, []);

  const start = useCallback(async () => {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      onError?.("This browser cannot record audio.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];

      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => {
        releaseMic();
        setRecording(false);
        setSeconds(0);
        const audio = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        chunksRef.current = [];
        // A zero-length clip is a mis-tap, not a message.
        if (audio.size > 0) onRecorded(audio);
      };

      recorder.start();
      setRecording(true);
      setSeconds(0);
      tickRef.current = setInterval(() => {
        setSeconds((s) => {
          if (s + 1 >= MAX_SECONDS) stop();
          return s + 1;
        });
      }, 1000);
    } catch {
      releaseMic();
      onError?.("Microphone permission denied, or no microphone available.");
    }
  }, [onError, onRecorded, releaseMic, stop]);

  // Navigating away mid-recording must not leave the microphone live.
  useEffect(() => releaseMic, [releaseMic]);

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => (recording ? stop() : void start())}
      aria-label={recording ? "Stop recording and send" : "Record a voice message"}
      title={recording ? "Stop and send" : "Record a voice message"}
      className={`grid h-[38px] w-[38px] shrink-0 place-items-center rounded-lg border transition-colors disabled:opacity-50 ${
        recording
          ? "border-red-500 bg-red-500/15 text-red-500"
          : "border-line bg-panel-2 text-txt-2 hover:text-txt"
      }`}
    >
      {recording ? (
        <span className="text-[10px] font-bold tabular-nums">{seconds}s</span>
      ) : (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
          <path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4" />
        </svg>
      )}
    </button>
  );
}
