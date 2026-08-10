"use client";

import { useEffect, useRef, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { VoiceRecorder } from "@/components/VoiceRecorder";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorState,
  LiveChip,
  Loading,

  inputClass,
} from "@/components/ui";
import { api, errorMessage, fmtDateTime, timeAgo, type DirectMessage } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useLiveFeed } from "@/lib/useLiveFeed";

const NO_MESSAGES: DirectMessage[] = [];

/**
 * The manager's side of the direct conversation with each worker.
 *
 * Separate from the questions inbox on purpose: a question is a formal request with an LLM answer
 * and a status to resolve, while this is just talking. Mixing them would bury an unanswered safety
 * question under "on my way".
 */
export default function MessagesPage() {
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const live = useLiveFeed({
    topics: ["message"],
    onFrame: () => {
      void threads.refresh();
      void thread.refresh();
    },
  });

  const threads = usePoll(() => api.messageThreads(), live.connected ? 20000 : 6000, [
    live.connected,
  ]);
  const thread = usePoll(
    () => (selected ? api.thread(selected) : Promise.resolve({ worker_id: "", messages: [] })),
    live.connected ? 20000 : 5000,
    [selected, live.connected],
  );

  const list = threads.data?.threads ?? [];
  const messages = selected ? (thread.data?.messages ?? NO_MESSAGES) : NO_MESSAGES;

  // Opening a conversation is what marks it read — not merely receiving it.
  useEffect(() => {
    if (!selected) return;
    void api
      .markThreadRead(selected)
      .then(() => threads.refresh())
      .catch(() => {
        // Not worth interrupting the manager over; the badge simply stays until the next open.
      });
  }, [selected, messages.length, threads]);

  const send = async (audio?: Blob) => {
    if (!selected) return;
    const text = draft.trim();
    if (!text && !audio) return;
    setSending(true);
    setSendError(null);
    try {
      await api.sendMessage(selected, { text, audio });
      setDraft("");
      await thread.refresh();
      await threads.refresh();
    } catch (e) {
      setSendError(errorMessage(e));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Messages"
        subtitle="Talk to a worker directly · type, or send a voice message"
        action={<LiveChip connected={live.connected} />}
      />

      {threads.error && !threads.data ? (
        <ErrorState message={threads.error} onRetry={() => void threads.refresh()} />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
          {/* ------------------------------------------------------- inbox */}
          <Card className="max-h-[70vh] overflow-y-auto p-0">
            {threads.loading && !threads.data ? (
              <Loading label="Loading conversations…" />
            ) : list.length === 0 ? (
              <Empty>No conversations yet. Workers appear here once they message you.</Empty>
            ) : (
              list.map((t) => (
                <button
                  key={t.worker_id}
                  type="button"
                  onClick={() => setSelected(t.worker_id)}
                  className={`flex w-full items-start gap-2.5 border-b border-line-soft px-3.5 py-3 text-left transition-colors last:border-0 ${
                    selected === t.worker_id ? "bg-accent/10" : "hover:bg-panel-2"
                  }`}
                >
                  <div className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-panel text-[11px] font-semibold text-txt-2">
                    {(t.worker_name ?? t.worker_id).slice(0, 1).toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-sm font-semibold text-txt">
                        {t.worker_name ?? t.worker_id}
                      </span>
                      {t.unread > 0 ? <Badge tone="bad">{t.unread}</Badge> : null}
                    </div>
                    <div className="truncate text-[11px] text-txt-3">
                      {t.last_sender_role === "site_manager" ? "You: " : ""}
                      {t.last_has_audio && !t.last_text ? "🎤 Voice message" : t.last_text}
                    </div>
                    <div className="text-[10px] text-txt-3">{timeAgo(t.last_at)}</div>
                  </div>
                </button>
              ))
            )}
          </Card>

          {/* ------------------------------------------------------- conversation */}
          <div className="min-w-0">
            {!selected ? (
              <Card>
                <Empty>Pick a worker to open the conversation.</Empty>
              </Card>
            ) : (
              <>
                <Card className="mb-3 max-h-[52vh] overflow-y-auto px-4 py-3">
                  {messages.length === 0 ? (
                    <Empty>No messages yet — say something.</Empty>
                  ) : (
                    <div className="space-y-2.5">
                      {messages.map((m) => (
                        <MessageBubble key={m.message_id} message={m} />
                      ))}
                    </div>
                  )}
                </Card>

                <Card className="px-3.5 py-3">
                  <div className="flex items-end gap-2">
                    <textarea
                      rows={2}
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          void send();
                        }
                      }}
                      placeholder="Message the worker… (Enter to send)"
                      className={`${inputClass} flex-1 resize-none`}
                    />
                    <VoiceRecorder
                      disabled={sending}
                      onRecorded={(blob) => void send(blob)}
                      onError={setSendError}
                    />
                    <Button onClick={() => void send()} disabled={sending || !draft.trim()}>
                      {sending ? "Sending…" : "Send"}
                    </Button>
                  </div>
                  {sendError ? (
                    <p className="mt-2 text-[12px] text-red-500" role="alert">
                      {sendError}
                    </p>
                  ) : null}
                </Card>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function MessageBubble({ message }: { message: DirectMessage }) {
  const mine = message.sender_role === "site_manager";
  const audioRef = useRef<HTMLAudioElement | null>(null);

  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[75%] rounded-xl px-3 py-2 ${
          mine ? "bg-accent/15 text-txt" : "border border-line bg-panel-2 text-txt"
        }`}
      >
        {!mine ? (
          <div className="mb-0.5 text-[10px] font-semibold text-txt-3">{message.sender_name}</div>
        ) : null}

        {message.text ? <div className="text-sm whitespace-pre-wrap">{message.text}</div> : null}

        {message.audio_url ? (
          <audio
            ref={audioRef}
            controls
            preload="none"
            src={message.audio_url}
            className="mt-1.5 h-9 w-full max-w-[260px]"
          >
            <track kind="captions" />
          </audio>
        ) : null}

        <div className="mt-0.5 text-[10px] text-txt-3">{fmtDateTime(message.created_at)}</div>
      </div>
    </div>
  );
}

