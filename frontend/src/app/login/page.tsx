"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button, Card, Field, Note, inputClass } from "@/components/ui";
import { api, errorMessage } from "@/lib/api";
import { setSession } from "@/lib/session";

/**
 * The one login page for both roles. A worker who signs in here is told to use the mobile
 * app instead of being handed a broken dashboard — this Next.js app only has manager pages;
 * the worker experience is the Flutter app (see `worker_app/`).
 */
export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [workerNotice, setWorkerNotice] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setWorkerNotice(false);
    try {
      const { token, user } = await api.login({ username: username.trim(), password });
      if (user.role !== "site_manager") {
        // A valid login, just the wrong app — do not store a session that has nowhere to go.
        setWorkerNotice(true);
        return;
      }
      setSession(token, user);
      router.replace("/");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-panel-soft p-6">
      <Card className="w-full max-w-sm px-6 py-7">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-accent text-sm font-bold text-white">
            FP
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold">FieldPilot AI</div>
            <div className="text-[10px] uppercase tracking-wider text-txt-3">Site manager sign-in</div>
          </div>
        </div>

        {workerNotice ? (
          <Note tone="info" title="Use the FieldPilot Worker app">
            That account is a worker account. Sign in from the FieldPilot Worker app on your
            phone instead — this dashboard is for site managers.
          </Note>
        ) : null}
        {error ? (
          <Note tone="bad" title="Sign-in failed">
            {error}
          </Note>
        ) : null}

        <form onSubmit={(e) => void submit(e)} className="space-y-3.5">
          <Field label="Username" htmlFor="login-username">
            <input
              id="login-username"
              autoComplete="username"
              required
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className={inputClass}
            />
          </Field>
          <Field label="Password" htmlFor="login-password">
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={inputClass}
            />
          </Field>
          <Button type="submit" disabled={submitting || !username.trim() || !password} className="w-full">
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-5 text-center text-[11px] text-txt-3">
          Demo account · <code className="font-mono">manager</code> /{" "}
          <code className="font-mono">manager123</code>
        </p>
      </Card>
    </div>
  );
}
