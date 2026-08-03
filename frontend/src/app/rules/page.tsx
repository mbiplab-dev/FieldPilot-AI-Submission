"use client";

import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Card, Empty, ErrorState, Loading, Note } from "@/components/ui";
import { api, errorMessage, type Rule } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

export default function RulesPage() {
  const { data, error, loading, refresh } = usePoll(() => api.rules(), 10000);
  const [busy, setBusy] = useState<string | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);

  const rules = data?.rules ?? [];

  const toggle = async (r: Rule) => {
    setBusy(r.rule_id);
    setToggleError(null);
    try {
      await api.updateRule(r.rule_id, {
        name: r.name, enabled: !r.enabled, priority: r.priority,
        event_types: r.event_types, conditions: r.conditions, action: r.action,
        cooldown_s: r.cooldown_s,
      });
      await refresh();
    } catch (e) {
      setToggleError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  if (error && !data) {
    return (
      <div className="p-6">
        <PageHeader title="Rules engine" subtitle="Configurable IF → THEN rules" />
        <ErrorState message={error} onRetry={() => void refresh()} />
      </div>
    );
  }

  return (
    <div className="p-6">
      <PageHeader
        title="Rules engine"
        subtitle="Configurable IF → THEN rules · stored in PostgreSQL · evaluated on every new alert"
      />

      {error ? (
        <Note tone="warn" title="Showing the last good snapshot">
          {error}
        </Note>
      ) : null}
      {toggleError ? (
        <Note tone="bad" title="Could not update the rule">
          {toggleError}
        </Note>
      ) : null}

      <Card className="overflow-x-auto">
        <table className="w-full min-w-[760px]">
          <thead>
            <tr className="border-b border-line text-left">
              <Th>Rule</Th>
              <Th>Priority</Th>
              <Th>Event types</Th>
              <Th>Action</Th>
              <Th>Cooldown</Th>
              <Th>Enabled</Th>
              <Th>{" "}</Th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r: Rule) => (
              <tr key={r.rule_id} className="border-b border-line-soft text-sm last:border-0">
                <Td>
                  <div className="font-semibold">{r.name}</div>
                  <div className="text-[11px] text-txt-3">{r.conditions.length} condition(s)</div>
                </Td>
                <Td><span className="font-mono text-xs">{r.priority}</span></Td>
                <Td>
                  <div className="flex flex-wrap gap-1">
                    {(r.event_types.length ? r.event_types : ["*"]).map((t) => (
                      <span key={t} className="rounded border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-sky-500">{t}</span>
                    ))}
                  </div>
                </Td>
                <Td>
                  <span className="rounded border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] font-semibold text-amber-500">
                    {String(r.action.type ?? "–")}
                  </span>
                  {r.action.severity != null && (
                    <span className="ml-1.5 text-xs text-txt-3">{String(r.action.severity)}</span>
                  )}
                </Td>
                <Td><span className="font-mono text-xs text-txt-2">{r.cooldown_s}s</span></Td>
                <Td>{r.enabled
                  ? <span className="rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-semibold text-emerald-500">on</span>
                  : <span className="rounded border border-zinc-400/30 bg-zinc-400/10 px-2 py-0.5 text-[11px] font-semibold text-zinc-400">off</span>}
                </Td>
                <Td>
                  <button
                    type="button"
                    onClick={() => void toggle(r)}
                    disabled={busy === r.rule_id}
                    aria-label={`${r.enabled ? "Disable" : "Enable"} rule ${r.name}`}
                    className="rounded-lg border border-line bg-panel-2 px-2.5 py-1 text-[11px] font-semibold text-txt-2 hover:text-txt disabled:opacity-50"
                  >
                    {r.enabled ? "Disable" : "Enable"}
                  </button>
                </Td>
              </tr>
            ))}
            {rules.length === 0 && (
              <tr>
                <td colSpan={7}>
                  {loading ? (
                    <Loading label="Loading rules…" />
                  ) : (
                    <Empty>No rules configured — the rules engine has nothing to evaluate.</Empty>
                  )}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3.5 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-txt-3">{children}</th>;
}
function Td({ children }: { children: React.ReactNode }) {
  return <td className="px-3.5 py-2.5 text-[13px] align-middle">{children}</td>;
}