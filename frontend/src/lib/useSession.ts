"use client";

import { useSyncExternalStore } from "react";
import { readSession, serverSession, subscribeSession, type Session } from "./session";

/** React binding for the external session store — see `session.ts` for why it lives there. */
export function useSession(): Session {
  return useSyncExternalStore(subscribeSession, readSession, serverSession);
}
