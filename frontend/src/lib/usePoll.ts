"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";

export interface Poll<T> {
  /** Last successfully loaded value — kept across failures so the UI stays useful. */
  data: T | undefined;
  /** Message from the most recent failure, or `null` when the last load succeeded. */
  error: string | null;
  /** True until the first load settles. */
  loading: boolean;
  refresh: () => Promise<void>;
}

/**
 * Polls a loader function every `interval` ms, pausing when the tab is hidden.
 *
 * Pass a longer `interval` when a websocket is delivering updates — polling then
 * acts as a safety net rather than the primary transport. Changing `interval`
 * restarts the timer.
 */
export function usePoll<T>(
  loader: () => Promise<T>,
  interval: number,
  deps: unknown[] = [],
): Poll<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);

  const refresh = useCallback(async () => {
    try {
      const next = await loaderRef.current();
      setData(next);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loaderRef.current = loader;
    void refresh();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, interval);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interval, ...deps]);

  return { data, error, loading, refresh };
}
