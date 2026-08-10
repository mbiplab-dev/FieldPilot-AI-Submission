"use client";

import { createContext, useCallback, useContext, useState } from "react";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useLiveFeed } from "@/lib/useLiveFeed";

interface Badges {
  /** Worker questions still awaiting a manager's answer. */
  questions: number;
  /** Unread direct messages from workers. */
  messages: number;
  /** Alerts currently NEW or ACTIVE. */
  alerts: number;
  refresh: () => void;
}

const BadgeContext = createContext<Badges>({
  questions: 0,
  messages: 0,
  alerts: 0,
  refresh: () => {},
});

export const useNavBadges = (): Badges => useContext(BadgeContext);

/**
 * Counts the things waiting for the site manager, so the sidebar can say so.
 *
 * A worker's question used to arrive silently: it landed in the questions inbox and stayed there
 * until somebody happened to open that page. On a live site "somebody asked whether this is safe"
 * is exactly the thing that must not wait for a page visit, so it is counted here and pushed the
 * moment it arrives rather than on the next poll.
 */
export function NavBadgeProvider({ children }: { children: React.ReactNode }) {
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  // Anything that changes a count re-reads it immediately; polling is the safety net.
  useLiveFeed({
    topics: ["question", "message", "alert", "alert_resolved"],
    onFrame: refresh,
  });

  const questions = usePoll(() => api.questionStats(), 30000, [nonce]);
  const messages = usePoll(() => api.unreadMessages(), 30000, [nonce]);
  const alerts = usePoll(() => api.alerts({ state: "ACTIVE", limit: "200" }), 30000, [nonce]);

  const value: Badges = {
    questions: questions.data?.pending ?? 0,
    messages: messages.data?.unread ?? 0,
    alerts: alerts.data?.alerts?.length ?? 0,
    refresh,
  };

  return <BadgeContext.Provider value={value}>{children}</BadgeContext.Provider>;
}
