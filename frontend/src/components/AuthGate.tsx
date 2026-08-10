"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { NavBadgeProvider } from "./NavBadges";
import { Sidebar } from "./Sidebar";
import { VoiceAnnouncer } from "./VoiceAnnouncer";
import { WorkerAlarmBanner } from "./WorkerAlarmBanner";
import { useSession } from "@/lib/useSession";

/**
 * Gates every page behind a signed-in site_manager session.
 *
 * This dashboard is manager-only now that workers have the FieldPilot Worker mobile app (see
 * `worker_app/`) — a worker account CAN sign in (the backend accepts it), but there is nowhere
 * for it to go here, so it gets a plain notice instead of a broken dashboard.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const pathname = usePathname();
  const router = useRouter();
  const isLoginRoute = pathname === "/login";

  useEffect(() => {
    if (!session.hydrated || isLoginRoute) return;
    if (!session.user) router.replace("/login");
  }, [session.hydrated, session.user, isLoginRoute, router]);

  if (isLoginRoute) return <>{children}</>;

  // Not yet hydrated, or hydrated-but-signed-out (redirect is in flight): render nothing
  // dashboard-shaped rather than flash real content behind the login wall.
  if (!session.hydrated || !session.user) return <FullScreenStatus label="Loading…" />;

  if (session.user.role !== "site_manager") {
    return (
      <FullScreenStatus
        label="This dashboard is for site managers"
        detail={`Signed in as ${session.user.display_name} (worker). Use the FieldPilot Worker app on your phone instead.`}
      />
    );
  }

  return (
    <NavBadgeProvider>
      <div className="flex min-h-screen">
        {/* Both mounted above the pages: a hazard must be spoken, and a worker raising the alarm
            must be seen, whichever page the manager happens to be on. */}
        <VoiceAnnouncer />
        <WorkerAlarmBanner />
        <Sidebar />
        <main className="flex-1 overflow-x-hidden">{children}</main>
      </div>
    </NavBadgeProvider>
  );
}

function FullScreenStatus({ label, detail }: { label: string; detail?: string }) {
  return (
    <div className="grid min-h-screen place-items-center bg-panel-soft p-6 text-center">
      <div>
        <div className="text-sm font-semibold text-txt">{label}</div>
        {detail ? <div className="mt-1.5 max-w-xs text-[12px] text-txt-3">{detail}</div> : null}
      </div>
    </div>
  );
}
