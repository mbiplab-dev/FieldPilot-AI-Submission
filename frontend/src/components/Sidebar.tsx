"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ThemeToggle } from "./ThemeToggle";

const NAV = [
  { href: "/", label: "Overview", icon: "M3 12 12 3l9 9M5 10v10h4v-6h6v6h4V10" },
  { href: "/live", label: "Live", icon: "M23 7 16 12l7 5V7zm-9 1a4 4 0 1 1-4 4 4 4 0 0 1 4-4zM1 5h14v14H1a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" },
  { href: "/alerts", label: "Alerts", icon: "M18 8A6 6 0 0 0 6 8 5 5 0 0 0 5 22h13a5 5 0 0 0 0-14M12 9v4m0 4h.01" },
  { href: "/rfis", label: "RFIs", icon: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zm0 0v6h6M9 15l2 2 4-4" },
  { href: "/workers", label: "Workers", icon: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm14 10v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" },
  { href: "/zones", label: "Zones", icon: "M12 21s7-6.4 7-11a7 7 0 1 0-14 0c0 4.6 7 11 7 11zm0-8a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" },
  { href: "/rules", label: "Rules", icon: "M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" },
  { href: "/learning", label: "Learning", icon: "M3 17l6-6 4 4 8-8M14 7h7v7" },
  { href: "/blueprints", label: "Blueprints", icon: "M3 6h18M3 6v13a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V6M3 6l2-3h14l2 3M9 10v6m6-6v6" },
  { href: "/activity", label: "Activity", icon: "M12 6v6l4 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z" },
];

export function Sidebar({ alertCount = 0 }: { alertCount?: number }) {
  const pathname = usePathname();

  return (
    <aside className="flex h-screen w-60 flex-col border-r border-line bg-panel-soft">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <div className="grid h-8 w-8 place-items-center rounded-lg bg-accent text-sm font-bold text-white">
          FP
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold">FieldPilot AI</div>
          <div className="text-[10px] uppercase tracking-wider text-txt-3">Operations</div>
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 overflow-y-auto px-2">
        {NAV.map((item) => {
          const active =
            item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                active
                  ? "bg-accent/10 font-semibold text-accent"
                  : "text-txt-2 hover:bg-panel hover:text-txt"
              }`}
            >
              <svg
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="shrink-0"
              >
                <path d={item.icon} />
              </svg>
              <span className="flex-1">{item.label}</span>
              {item.href === "/alerts" && alertCount > 0 && (
                <span className="rounded-full bg-accent/15 px-1.5 text-[10px] font-bold text-accent">
                  {alertCount}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="flex items-center justify-between border-t border-line px-4 py-3">
        <span className="text-[11px] text-txt-3">Theme</span>
        <ThemeToggle />
      </div>
    </aside>
  );
}