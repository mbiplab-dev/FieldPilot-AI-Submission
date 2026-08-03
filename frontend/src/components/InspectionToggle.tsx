"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * Inspection mode toggle — turns the trained structural-damage detector on/off
 * at the edge via the `control.inspection` bus channel.
 */
export function InspectionToggle({ variant = "switch" }: { variant?: "switch" | "button" }) {
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.inspectionMode().then((s) => setEnabled(s.enabled)).catch(() => {});
    const t = setInterval(() => {
      api.inspectionMode().then((s) => setEnabled(s.enabled)).catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, []);

  const toggle = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.setInspectionMode(!enabled);
      setEnabled(r.enabled);
    } catch {
      setError("Failed — backend reachable?");
    } finally {
      setLoading(false);
    }
  };

  if (variant === "button") {
    return (
      <button
        onClick={toggle}
        disabled={loading}
        className={`rounded-lg border px-3 py-1.5 text-xs font-semibold transition-colors ${
          enabled
            ? "border-purple-500/40 bg-purple-500/10 text-purple-500 hover:bg-purple-500/20"
            : "border-line bg-panel-2 text-txt-2 hover:text-txt"
        }`}
        title={error ?? "Toggle structural-damage inspection mode"}
      >
        Inspection {enabled ? "ON" : "OFF"}
      </button>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <button
        onClick={toggle}
        disabled={loading}
        className={`relative h-6 w-11 rounded-full border transition-colors ${
          enabled
            ? "border-purple-500/40 bg-purple-500/30"
            : "border-line bg-panel-2"
        }`}
        title="Toggle structural-damage inspection mode"
      >
        <span
          className={`absolute top-0.5 h-4.5 w-4.5 rounded-full transition-all duration-200 ${
            enabled
              ? "left-[22px] bg-purple-500"
              : "left-0.5 bg-zinc-400"
          }`}
          style={{ height: "18px", width: "18px" }}
        />
      </button>
      <div>
        <div className="text-sm font-semibold">
          Inspection mode{" "}
          <span className={enabled ? "text-purple-500" : "text-txt-3"}>
            {enabled ? "ON" : "OFF"}
          </span>
        </div>
        <div className="text-[11px] text-txt-3">
          {error ?? (enabled
            ? "Structural-damage detector active — cracks publish as events."
            : "Toggle on to scan for cracks, rust, missing bolts…")}
        </div>
      </div>
    </div>
  );
}