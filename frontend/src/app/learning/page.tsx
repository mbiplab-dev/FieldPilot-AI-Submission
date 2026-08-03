"use client";

import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import {
  Badge,
  Button,
  Card,
  Delta,
  Empty,
  ErrorState,
  Field,
  LiveChip,
  Loading,
  Note,
  SectionTitle,
  StatTile,
  Td,
  Th,
  inputClass,
  type Tone,
} from "@/components/ui";
import {
  api,
  errorMessage,
  fmtDateTime,
  fmtDelta,
  fmtMetric,
  fmtPercent,
  timeAgo,
  type FeedbackStats,
  type LearningRun,
  type LearningStatus,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { frameLearningRun, useLiveFeed } from "@/lib/useLiveFeed";

const STATUS_TONE: Record<LearningStatus, Tone> = {
  pending: "info",
  running: "accent",
  completed: "good",
  failed: "bad",
  blocked: "warn",
};

function isInFlight(run: LearningRun | null | undefined): boolean {
  return run?.status === "running" || run?.status === "pending";
}

export default function LearningPage() {
  const [epochs, setEpochs] = useState("");
  const [training, setTraining] = useState(false);
  const [trainError, setTrainError] = useState<string | null>(null);
  const [triggered, setTriggered] = useState<LearningRun | null>(null);

  // Drives the fast poll while a run is in flight — updated from the loader and
  // from live `learning` frames, never during render.
  const [inFlight, setInFlight] = useState(false);

  const live = useLiveFeed({
    topics: ["learning"],
    onFrame: (frame) => {
      const run = frameLearningRun(frame);
      if (run) setInFlight(isInFlight(run));
      void refresh();
    },
  });

  const { data, error, loading, refresh } = usePoll(
    async () => {
      const runsResult = await api.learningRuns(25);
      setInFlight(isInFlight(runsResult.runs[0]));
      // Stats live behind a separate endpoint; a miss there must not hide the runs.
      let stats: FeedbackStats | null = null;
      let statsError: string | null = null;
      try {
        stats = await api.feedbackStats();
      } catch (e) {
        statsError = errorMessage(e);
      }
      return { runs: runsResult.runs, stats, statsError };
    },
    // Poll hard while a run is in flight, regardless of the socket.
    inFlight ? 3000 : live.connected ? 30000 : 12000,
    [inFlight, live.connected],
  );

  const runs = data?.runs ?? [];
  const latest = runs[0] ?? null;
  const stats = data?.stats ?? null;
  const banner = triggered ?? latest;

  const train = async () => {
    setTraining(true);
    setTrainError(null);
    setTriggered(null);
    try {
      const parsed = Number.parseInt(epochs, 10);
      const run = await api.train(
        Number.isFinite(parsed) && parsed > 0 ? { epochs: parsed } : {},
      );
      setTriggered(run);
      setInFlight(isInFlight(run));
      await refresh();
    } catch (e) {
      setTrainError(errorMessage(e));
    } finally {
      setTraining(false);
    }
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Learning loop"
        subtitle="Supervisor feedback → fine-tune → mAP50 gate · new weights ship only when they beat the incumbent"
        action={<LiveChip connected={live.connected} />}
      />

      <Card className="mb-6 px-4 py-4">
        <div className="flex flex-wrap items-end gap-4">
          <Field
            label="Epochs"
            htmlFor="learning-epochs"
            hint="Leave blank for the backend default"
            className="w-40"
          >
            <input
              id="learning-epochs"
              type="number"
              min={1}
              max={500}
              inputMode="numeric"
              value={epochs}
              onChange={(e) => setEpochs(e.target.value)}
              placeholder="auto"
              className={inputClass}
            />
          </Field>
          <div className="pb-0.5">
            <Button onClick={() => void train()} disabled={training || isInFlight(latest)}>
              {training ? "Starting…" : isInFlight(latest) ? "Run in progress…" : "Train now"}
            </Button>
          </div>
          <p className="max-w-md pb-1 text-[12px] text-txt-3">
            Training consumes the approved/rejected alerts below, then evaluates the candidate
            against the locked validation set. Weights are promoted only on a positive mAP50 delta.
          </p>
        </div>
        {trainError ? (
          <p className="mt-2.5 text-[12px] text-red-500" role="alert">
            {trainError}
          </p>
        ) : null}
      </Card>

      {banner ? <RunBanner run={banner} /> : null}

      <SectionTitle>Supervisor feedback</SectionTitle>
      {data?.statsError ? (
        <Note tone="warn" title="Feedback stats unavailable">
          {data.statsError}
        </Note>
      ) : null}
      <div className="grid grid-cols-2 gap-3.5 md:grid-cols-3 lg:grid-cols-5">
        <StatTile value={stats ? stats.total : "–"} label="Reviewed alerts" />
        <StatTile value={stats ? stats.approved : "–"} label="Approved" accent="#10b981" />
        <StatTile value={stats ? stats.rejected : "–"} label="Rejected" accent="#ef4444" />
        <StatTile
          value={stats ? fmtPercent(stats.approval_rate) : "–"}
          label="Approval rate"
          accent="#2f6fdd"
        />
        <StatTile
          value={stats ? stats.unconsumed : "–"}
          label="Not yet trained on"
          accent="#f59e0b"
        />
      </div>

      <SectionTitle>Run history</SectionTitle>
      {error && !data ? (
        <ErrorState message={error} onRetry={() => void refresh()} />
      ) : loading && !data ? (
        <Card>
          <Loading label="Loading runs…" />
        </Card>
      ) : (
        <>
          {error ? (
            <Note tone="warn" title="Showing cached runs">
              {error}
            </Note>
          ) : null}
          <Card className="overflow-x-auto">
            <table className="w-full min-w-[900px]">
              <thead>
                <tr className="border-b border-line text-left">
                  <Th>Started</Th>
                  <Th>Status</Th>
                  <Th>Samples</Th>
                  <Th>Epochs</Th>
                  <Th>mAP50 before</Th>
                  <Th>mAP50 after</Th>
                  <Th>Delta</Th>
                  <Th>Promoted</Th>
                  <Th>Detail</Th>
                </tr>
              </thead>
              <tbody>
                {runs.length ? (
                  runs.map((run) => (
                    <tr key={run.run_id} className="border-b border-line-soft last:border-0">
                      <Td>
                        <div>{fmtDateTime(run.created_at)}</div>
                        <div className="font-mono text-[11px] text-txt-3">{run.run_id}</div>
                      </Td>
                      <Td>
                        <Badge tone={STATUS_TONE[run.status] ?? "neutral"}>{run.status}</Badge>
                      </Td>
                      <Td>
                        <span className="font-mono">{run.samples}</span>
                      </Td>
                      <Td>
                        <span className="font-mono">{run.epochs}</span>
                      </Td>
                      <Td>
                        <span className="font-mono tabular-nums">{fmtMetric(run.map50_before)}</span>
                      </Td>
                      <Td>
                        <span className="font-mono tabular-nums">{fmtMetric(run.map50_after)}</span>
                      </Td>
                      <Td>
                        <Delta value={run.delta} text={fmtDelta(run.delta)} />
                      </Td>
                      <Td>
                        {run.promoted ? (
                          <Badge tone="good">promoted</Badge>
                        ) : (
                          <Badge tone="neutral">kept old weights</Badge>
                        )}
                      </Td>
                      <Td className="max-w-[320px]">
                        <span className="text-txt-2">{run.message || "–"}</span>
                        {run.weights_path ? (
                          <div className="truncate font-mono text-[11px] text-txt-3">
                            {run.weights_path}
                          </div>
                        ) : null}
                      </Td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <Td colSpan={9}>
                      <Empty>
                        No training runs yet — approve or reject alerts, then press “Train now”.
                      </Empty>
                    </Td>
                  </tr>
                )}
              </tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  );
}

/** Loud, plain-language status for the most relevant run. */
function RunBanner({ run }: { run: LearningRun }) {
  if (run.status === "blocked") {
    return (
      <Note tone="warn" title="Training did not start">
        {run.message || "The learning loop declined this run."}
      </Note>
    );
  }
  if (run.status === "failed") {
    return (
      <Note tone="bad" title="Training failed">
        {run.message || "The run stopped with an error."}
      </Note>
    );
  }
  if (isInFlight(run)) {
    return (
      <Note tone="info" title={run.status === "running" ? "Training in progress" : "Run queued"}>
        {run.samples} sample(s) · {run.epochs} epoch(s) · started {timeAgo(run.created_at)}
        {run.message ? ` · ${run.message}` : ""}
      </Note>
    );
  }
  return (
    <Note tone={run.promoted ? "good" : "neutral"} title={run.promoted ? "Weights promoted" : "Weights kept"}>
      mAP50 {fmtMetric(run.map50_before)} → {fmtMetric(run.map50_after)} (delta{" "}
      {fmtDelta(run.delta)}) · finished {timeAgo(run.finished_at ?? run.created_at)}
      {run.message ? ` · ${run.message}` : ""}
    </Note>
  );
}
