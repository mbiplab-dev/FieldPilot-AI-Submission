"use client";

/**
 * Shared indication components — the ONLY places color appears in the UI.
 * Severity / state semantics:
 *   critical→red  high→orange  medium→amber  low→green
 *   NEW→blue  ACTIVE→amber  RESOLVED→emerald  SUPPRESSED→gray
 */

import type { AlertState, Severity } from "@/lib/api";

const SEV_STYLE: Record<Severity, string> = {
  critical: "text-red-500 bg-red-500/10 border-red-500/30",
  high: "text-orange-500 bg-orange-500/10 border-orange-500/30",
  medium: "text-amber-500 bg-amber-500/10 border-amber-500/30",
  low: "text-lime-600 dark:text-lime-400 bg-lime-500/10 border-lime-500/30",
};

const STATE_STYLE: Record<AlertState, string> = {
  NEW: "text-sky-500 bg-sky-500/10 border-sky-500/30",
  ACTIVE: "text-amber-500 bg-amber-500/10 border-amber-500/30",
  RESOLVED: "text-emerald-500 bg-emerald-500/10 border-emerald-500/30",
  SUPPRESSED: "text-zinc-400 bg-zinc-400/10 border-zinc-400/30",
};

export function Chip({ label, className }: { label: string; className: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold whitespace-nowrap ${className}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {label}
    </span>
  );
}

export function SeverityChip({ severity }: { severity: Severity }) {
  return <Chip label={severity} className={SEV_STYLE[severity] ?? SEV_STYLE.medium} />;
}

export function StateChip({ state }: { state: AlertState }) {
  return <Chip label={state} className={STATE_STYLE[state] ?? STATE_STYLE.NEW} />;
}

export function StatusChip({ status }: { status: string }) {
  const cls =
    status === "at_risk"
      ? SEV_STYLE.critical
      : status === "flagged"
        ? SEV_STYLE.medium
        : "text-emerald-500 bg-emerald-500/10 border-emerald-500/30";
  return <Chip label={status.replace("_", " ")} className={cls} />;
}

export function Card({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-xl border border-line bg-panel shadow-sm ${className}`}>
      {children}
    </div>
  );
}

export function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-2.5 mt-6 text-[11px] font-semibold uppercase tracking-[0.08em] text-txt-3 first:mt-0">
      {children}
    </h2>
  );
}

export function StatTile({
  value,
  label,
  accent,
}: {
  value: React.ReactNode;
  label: string;
  accent?: string;
}) {
  return (
    <Card className="px-4 py-3.5">
      <div className="flex items-stretch gap-3">
        {accent ? <div className="w-1 self-stretch rounded-full" style={{ background: accent }} /> : null}
        <div>
          <div className="text-2xl font-bold tabular-nums leading-tight">{value}</div>
          <div className="mt-0.5 text-[11.5px] text-txt-2">{label}</div>
        </div>
      </div>
    </Card>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-7 text-center text-[13px] text-txt-3">{children}</div>;
}

/* --------------------------- generic indications --------------------------- */

export type Tone = "neutral" | "info" | "good" | "warn" | "bad" | "accent" | "purple";

const TONE_STYLE: Record<Tone, string> = {
  neutral: "text-zinc-500 dark:text-zinc-400 bg-zinc-400/10 border-zinc-400/30",
  info: "text-sky-600 dark:text-sky-400 bg-sky-500/10 border-sky-500/30",
  good: "text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/30",
  warn: "text-amber-600 dark:text-amber-400 bg-amber-500/10 border-amber-500/30",
  bad: "text-red-600 dark:text-red-400 bg-red-500/10 border-red-500/30",
  accent: "text-accent bg-accent/10 border-accent/30",
  purple: "text-purple-600 dark:text-purple-400 bg-purple-500/10 border-purple-500/30",
};

/** Square-ish pill used for inline metadata (channel, status, event type…). */
export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: React.ReactNode;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] font-semibold whitespace-nowrap ${TONE_STYLE[tone]}`}
    >
      {children}
    </span>
  );
}

/** Full-width note used for errors, warnings and hints above content. */
export function Note({
  tone = "warn",
  title,
  children,
}: {
  tone?: Tone;
  title?: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      role={tone === "bad" ? "alert" : "status"}
      className={`mb-4 rounded-xl border px-4 py-3 text-[13px] ${TONE_STYLE[tone]}`}
    >
      {title ? <span className="font-semibold">{title}</span> : null}
      {title && children ? " · " : null}
      {children}
    </div>
  );
}

/** Shown when a fetch failed and there is nothing cached to fall back to. */
export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Card className="px-5 py-8 text-center">
      <div className="text-sm font-semibold text-red-500">Could not reach the backend</div>
      <p className="mx-auto mt-1.5 max-w-md text-[13px] text-txt-2">{message}</p>
      <p className="mt-1 text-[11px] text-txt-3">
        Start it with <code className="font-mono">make backend</code> — the dashboard proxies{" "}
        <code className="font-mono">/api</code> to port 8100.
      </p>
      {onRetry ? (
        <div className="mt-4">
          <Button onClick={onRetry} tone="secondary">
            Retry
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="py-7 text-center text-[13px] text-txt-3" role="status">
      {label}
    </div>
  );
}

/** Live-push indicator — degraded means the dashboard is polling instead. */
export function LiveChip({ connected }: { connected: boolean }) {
  return (
    <span
      title={
        connected
          ? "Receiving live push over websocket"
          : "Websocket disconnected — falling back to periodic polling"
      }
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold whitespace-nowrap ${
        connected ? TONE_STYLE.good : TONE_STYLE.warn
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full bg-current ${connected ? "animate-pulse" : ""}`}
      />
      {connected ? "live" : "degraded · polling"}
    </span>
  );
}

/** Signed metric delta — sign and colour must agree so a regression cannot read as a win. */
export function Delta({ value, text }: { value: number | null | undefined; text: string }) {
  const cls =
    value === null || value === undefined
      ? "text-txt-3"
      : value > 0
        ? "text-emerald-600 dark:text-emerald-400"
        : value < 0
          ? "text-red-600 dark:text-red-400"
          : "text-txt-2";
  return <span className={`font-mono font-semibold tabular-nums ${cls}`}>{text}</span>;
}

/* ------------------------------- form + table ------------------------------- */

const BUTTON_TONE: Record<"primary" | "secondary" | "good" | "bad", string> = {
  primary: "bg-accent text-white border-transparent hover:opacity-90",
  secondary: "border-line bg-panel-2 text-txt-2 hover:text-txt",
  good: "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/20",
  bad: "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400 hover:bg-red-500/20",
};

export function Button({
  children,
  onClick,
  tone = "primary",
  size = "md",
  type = "button",
  disabled,
  title,
  ariaLabel,
  ariaPressed,
  className = "",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  tone?: "primary" | "secondary" | "good" | "bad";
  size?: "sm" | "md";
  type?: "button" | "submit";
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
  ariaPressed?: boolean;
  className?: string;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel}
      aria-pressed={ariaPressed}
      className={`rounded-lg border font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        size === "sm" ? "px-2.5 py-1 text-[11.5px]" : "px-3.5 py-2 text-sm"
      } ${BUTTON_TONE[tone]} ${className}`}
    >
      {children}
    </button>
  );
}

export const inputClass =
  "w-full rounded-lg border border-line bg-panel px-3 py-1.5 text-sm text-txt placeholder:text-txt-3 focus:border-accent focus:outline-none";

/** Label + control pair; always renders a real `<label for>`. */
export function Field({
  label,
  htmlFor,
  hint,
  children,
  className = "",
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <label
        htmlFor={htmlFor}
        className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-txt-3"
      >
        {label}
      </label>
      {children}
      {hint ? <p className="mt-1 text-[11px] text-txt-3">{hint}</p> : null}
    </div>
  );
}

export function Th({
  children,
  className = "",
}: {
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={`px-3.5 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-txt-3 ${className}`}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  className = "",
  colSpan,
}: {
  children?: React.ReactNode;
  className?: string;
  colSpan?: number;
}) {
  return (
    <td colSpan={colSpan} className={`px-3.5 py-2.5 align-middle text-[13px] ${className}`}>
      {children}
    </td>
  );
}
