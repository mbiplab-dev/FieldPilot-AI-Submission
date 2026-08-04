"use client";

import { useMemo, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorState,
  Field,
  LiveChip,
  Loading,
  Note,
  SectionTitle,
  inputClass,
  type Tone,
} from "@/components/ui";
import { api, errorMessage, fmtDateTime, type QuestionStatus, type WorkerQuestion } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useLiveFeed } from "@/lib/useLiveFeed";

const STATUS_TONE: Record<QuestionStatus, Tone> = {
  pending: "warn",
  answered: "good",
  closed: "neutral",
};

const FILTERS: Array<{ value: QuestionStatus | ""; label: string }> = [
  { value: "", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "answered", label: "Answered" },
  { value: "closed", label: "Closed" },
];

// A stable reference so `data?.questions ?? EMPTY` doesn't invent a new array identity on
// every render while `data` is still undefined, which would otherwise re-run the `useMemo`
// below unnecessarily.
const EMPTY: WorkerQuestion[] = [];

/**
 * The manager's side of a worker asking "what is this, is it safe?" with a photo. The LLM
 * answers immediately and grounded where it can (see `reasoning/rfi.py`'s citation rules —
 * the same ones apply here); the manager reply is the authoritative one and is what actually
 * resolves the question.
 */
export default function QuestionsPage() {
  const [status, setStatus] = useState<QuestionStatus | "">("pending");
  const [openId, setOpenId] = useState<string | null>(null);
  const [reply, setReply] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const live = useLiveFeed({ topics: ["question"], onFrame: () => void refresh() });

  const { data, error, loading, refresh } = usePoll(
    () => api.questions({ status, limit: 200 }),
    live.connected ? 20000 : 6000,
    [live.connected, status],
  );

  const questions = data?.questions ?? EMPTY;
  const open = useMemo(
    () => questions.find((q) => q.question_id === openId) ?? null,
    [questions, openId],
  );

  const startReply = (q: WorkerQuestion) => {
    setOpenId(q.question_id);
    setReply(q.manager_reply ?? "");
    setSendError(null);
  };

  const submitReply = async () => {
    if (!open || !reply.trim()) return;
    setSending(true);
    setSendError(null);
    try {
      await api.replyToQuestion(open.question_id, { reply: reply.trim() });
      setOpenId(null);
      setReply("");
      await refresh();
    } catch (e) {
      setSendError(errorMessage(e));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Worker questions"
        subtitle="Photo questions from the field · the LLM answers first, your reply is the one that counts"
        action={<LiveChip connected={live.connected} />}
      />

      <div className="mb-4 flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.value || "all"}
            type="button"
            onClick={() => setStatus(f.value)}
            className={`rounded-full border px-3 py-1 text-[12px] font-medium transition-colors ${
              status === f.value
                ? "border-accent bg-accent/10 text-accent"
                : "border-line text-txt-2 hover:bg-panel"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {error && !data ? (
        <ErrorState message={error} onRetry={() => void refresh()} />
      ) : loading && !data ? (
        <Card>
          <Loading label="Loading questions…" />
        </Card>
      ) : (
        <>
          {error ? (
            <Note tone="warn" title="Showing the last good list">
              {error}
            </Note>
          ) : null}

          {questions.length === 0 ? (
            <Card>
              <Empty>No questions in this filter.</Empty>
            </Card>
          ) : (
            <div className="space-y-3">
              {questions.map((q) => (
                <QuestionCard
                  key={q.question_id}
                  question={q}
                  expanded={openId === q.question_id}
                  onToggle={() => (openId === q.question_id ? setOpenId(null) : startReply(q))}
                  reply={reply}
                  onReplyChange={setReply}
                  onSubmitReply={() => void submitReply()}
                  sending={sending}
                  sendError={openId === q.question_id ? sendError : null}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function QuestionCard({
  question,
  expanded,
  onToggle,
  reply,
  onReplyChange,
  onSubmitReply,
  sending,
  sendError,
}: {
  question: WorkerQuestion;
  expanded: boolean;
  onToggle: () => void;
  reply: string;
  onReplyChange: (v: string) => void;
  onSubmitReply: () => void;
  sending: boolean;
  sendError: string | null;
}) {
  const grounded = question.llm_grounded;
  return (
    <Card className="overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={`question-${question.question_id}`}
        className="flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-panel-2"
      >
        {question.image_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={question.image_url}
            alt=""
            className="h-14 w-14 shrink-0 rounded-lg border border-line object-cover"
          />
        ) : (
          <div className="grid h-14 w-14 shrink-0 place-items-center rounded-lg border border-line-soft text-txt-3">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 10c0 3.87-3.58 7-8 7a8.7 8.7 0 0 1-2.4-.33L3 18l1.24-3.09A6.9 6.9 0 0 1 2 10c0-3.87 3.58-7 8-7s8 3.13 8 7z" />
            </svg>
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={STATUS_TONE[question.status]}>{question.status}</Badge>
            {question.llm_grounded === false ? <Badge tone="bad">ungrounded answer</Badge> : null}
            <span className="font-mono text-[11px] text-txt-3">{question.zone ?? "no zone"}</span>
            <span className="ml-auto text-[11px] text-txt-3">{fmtDateTime(question.created_at)}</span>
          </div>
          <div className="mt-1 truncate text-sm font-medium text-txt">{question.text}</div>
          <div className="text-[11px] text-txt-3">Worker {question.worker_id}</div>
        </div>
      </button>

      {expanded ? (
        <div id={`question-${question.question_id}`} className="border-t border-line-soft px-4 py-3.5">
          <SectionTitle>Question</SectionTitle>
          <p className="mb-3 text-sm text-txt-2">{question.text}</p>

          {question.image_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={question.image_url}
              alt="Worker submitted photo"
              className="mb-3 max-h-64 w-full rounded-lg border border-line object-contain"
            />
          ) : null}

          {question.llm_answer ? (
            <div className="mb-3">
              <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-txt-3">
                Automated answer
                {grounded === false ? <Badge tone="bad">not backed by site documents</Badge> : null}
                {grounded ? <Badge tone="good">grounded</Badge> : null}
              </div>
              <p className="rounded-lg bg-panel-2 px-3 py-2.5 text-sm text-txt-2">{question.llm_answer}</p>
              {question.citations.length > 0 ? (
                <ul className="mt-1.5 space-y-0.5 text-[11px] text-txt-3">
                  {question.citations.map((c, i) => (
                    <li key={i}>
                      [{i + 1}] {c.citation}
                      {c.clause ? ` — clause ${c.clause}` : ""}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : (
            <Note tone="info">Automated answer not generated yet.</Note>
          )}

          <Field label="Your reply" htmlFor={`reply-${question.question_id}`} className="mt-3">
            <textarea
              id={`reply-${question.question_id}`}
              rows={3}
              value={reply}
              onChange={(e) => onReplyChange(e.target.value)}
              placeholder="Answer the worker directly — this is what they'll see as the authoritative answer."
              className={`${inputClass} resize-none`}
            />
          </Field>
          {sendError ? (
            <p className="mt-1.5 text-[12px] text-red-500" role="alert">
              {sendError}
            </p>
          ) : null}
          <div className="mt-2 flex justify-end">
            <Button onClick={onSubmitReply} disabled={sending || !reply.trim()}>
              {sending ? "Sending…" : question.manager_reply ? "Update reply" : "Send reply"}
            </Button>
          </div>

          {question.manager_reply ? (
            <p className="mt-3 border-t border-line-soft pt-2.5 text-[11px] text-txt-3">
              Last replied {fmtDateTime(question.replied_at)}
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}
