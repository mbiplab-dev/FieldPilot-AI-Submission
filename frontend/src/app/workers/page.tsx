"use client";

import { useState } from "react";
import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorState,
  Field,
  Loading,
  Note,
  SectionTitle,
  Td,
  Th,
  inputClass,
} from "@/components/ui";
import { api, errorMessage, type AuthUser, type WorkerSummary } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

interface Draft {
  username: string;
  password: string;
  display_name: string;
  worker_id: string;
}

const EMPTY_DRAFT: Draft = { username: "", password: "", display_name: "", worker_id: "" };

export default function WorkersPage() {
  const { data, error, loading, refresh } = usePoll(() => api.workers(), 5000);
  const {
    data: usersData,
    error: usersError,
    refresh: refreshUsers,
  } = usePoll(() => api.users(), 30000);

  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [created, setCreated] = useState<AuthUser | null>(null);

  // sort by active alerts desc — copy first, never mutate the polled payload
  const workers = [...(data?.workers ?? [])].sort((a, b) => b.active_alerts - a.active_alerts);
  const accounts = (usersData?.users ?? []).filter((u) => u.role === "worker");

  const submitCreate = async () => {
    if (!draft.username.trim() || draft.password.length < 8) {
      setCreateError("Username and an 8+ character password are required.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    setCreated(null);
    try {
      const user = await api.createUser({
        username: draft.username.trim(),
        password: draft.password,
        role: "worker",
        display_name: draft.display_name.trim() || undefined,
        worker_id: draft.worker_id.trim() || undefined,
      });
      setCreated(user);
      setDraft(EMPTY_DRAFT);
      await refreshUsers();
    } catch (e) {
      setCreateError(errorMessage(e));
    } finally {
      setCreating(false);
    }
  };

  if (error && !data) {
    return (
      <div className="p-6">
        <PageHeader title="Workers" subtitle="Live status, safety score, and accounts per worker" />
        <ErrorState message={error} onRetry={() => void refresh()} />
      </div>
    );
  }

  return (
    <div className="p-6">
      <PageHeader title="Workers" subtitle="Live status and safety score per worker · click for the full timeline" />
      {error ? (
        <Note tone="warn" title="Showing the last good snapshot">
          {error}
        </Note>
      ) : null}

      <Card>
        <table className="w-full">
          <thead>
            <tr className="border-b border-line text-left">
              <Th>Worker</Th>
              <Th>Zone</Th>
              <Th>Active alerts</Th>
              <Th>Open</Th>
            </tr>
          </thead>
          <tbody>
            {workers.length ? (
              workers.map((w: WorkerSummary) => (
                <tr
                  key={w.worker_id}
                  className="border-b border-line-soft transition-colors last:border-0 hover:bg-panel-2"
                >
                  <Td>
                    <Link href={`/workers/${encodeURIComponent(w.worker_id)}`} className="font-mono text-xs text-accent hover:underline">
                      {w.worker_id}
                    </Link>
                  </Td>
                  <Td><span className="font-mono text-xs text-txt-2">{w.zone ?? "–"}</span></Td>
                  <Td><span className="font-mono text-sm">{w.active_alerts}</span></Td>
                  <Td>
                    <Link href={`/workers/${encodeURIComponent(w.worker_id)}`} className="text-sm text-accent hover:underline">
                      View timeline →
                    </Link>
                  </Td>
                </tr>
              ))
            ) : (
              <tr>
                <Td colSpan={4}>
                  {loading ? (
                    <Loading label="Loading workers…" />
                  ) : (
                    <Empty>
                      No workers seen yet — events carry worker_id when the edge tracks people.
                    </Empty>
                  )}
                </Td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>

      <SectionTitle>Worker accounts</SectionTitle>
      <p className="mb-3 -mt-1 text-[12px] text-txt-3">
        Onboard a worker with a login for the FieldPilot Worker mobile app. `Worker id` is what
        links this account to their alerts and zone check-ins (the convention is{" "}
        <code className="font-mono">w-&lt;n&gt;</code>) — leave it blank if this account only
        needs to sign in without a linked identity yet.
      </p>

      <Card className="mb-6 px-4 py-4">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submitCreate();
          }}
        >
          <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2 lg:grid-cols-4">
            <Field label="Username" htmlFor="worker-username">
              <input
                id="worker-username"
                required
                value={draft.username}
                onChange={(e) => setDraft({ ...draft, username: e.target.value })}
                placeholder="e.g. worker3"
                className={inputClass}
              />
            </Field>
            <Field label="Password" htmlFor="worker-password" hint="8+ characters">
              <input
                id="worker-password"
                type="password"
                required
                value={draft.password}
                onChange={(e) => setDraft({ ...draft, password: e.target.value })}
                className={inputClass}
              />
            </Field>
            <Field label="Display name" htmlFor="worker-name" hint="Optional">
              <input
                id="worker-name"
                value={draft.display_name}
                onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
                placeholder="Full name"
                className={inputClass}
              />
            </Field>
            <Field label="Worker id" htmlFor="worker-id" hint="Optional, e.g. w-3">
              <input
                id="worker-id"
                value={draft.worker_id}
                onChange={(e) => setDraft({ ...draft, worker_id: e.target.value })}
                placeholder="w-3"
                className={inputClass}
              />
            </Field>
          </div>
          <div className="mt-3.5 flex items-center gap-3">
            <Button type="submit" disabled={creating || !draft.username.trim() || draft.password.length < 8}>
              {creating ? "Creating…" : "Create worker account"}
            </Button>
            {created ? (
              <span className="text-[12px] text-emerald-500">
                Created {created.username} — share the password with them directly; it is not
                shown again.
              </span>
            ) : null}
          </div>
          {createError ? (
            <p className="mt-2.5 text-[12px] text-red-500" role="alert">
              {createError}
            </p>
          ) : null}
        </form>
      </Card>

      {usersError ? (
        <Note tone="warn" title="Could not load accounts">
          {usersError}
        </Note>
      ) : (
        <Card className="overflow-x-auto">
          <table className="w-full min-w-[600px]">
            <thead>
              <tr className="border-b border-line text-left">
                <Th>Username</Th>
                <Th>Display name</Th>
                <Th>Worker id</Th>
                <Th>Status</Th>
              </tr>
            </thead>
            <tbody>
              {accounts.length ? (
                accounts.map((u) => (
                  <tr key={u.user_id} className="border-b border-line-soft last:border-0">
                    <Td><span className="font-mono text-xs">{u.username}</span></Td>
                    <Td>{u.display_name}</Td>
                    <Td>
                      {u.worker_id ? (
                        <span className="font-mono text-xs text-txt-2">{u.worker_id}</span>
                      ) : (
                        <span className="text-txt-3">not linked</span>
                      )}
                    </Td>
                    <Td>
                      <Badge tone={u.active === false ? "neutral" : "good"}>
                        {u.active === false ? "disabled" : "active"}
                      </Badge>
                    </Td>
                  </tr>
                ))
              ) : (
                <tr>
                  <Td colSpan={4}>
                    <Empty>No worker accounts yet — create one above.</Empty>
                  </Td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
