"use client";

import { useState } from "react";
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
  Td,
  Th,
  inputClass,
  type Tone,
} from "@/components/ui";
import {
  api,
  errorMessage,
  fmtDateTime,
  timeAgo,
  type HazardLevel,
  type Zone,
  type ZoneCreate,
  type ZoneOccupancy,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useLiveFeed } from "@/lib/useLiveFeed";

const HAZARDS: HazardLevel[] = ["low", "medium", "high"];

const HAZARD_TONE: Record<HazardLevel, Tone> = {
  low: "good",
  medium: "warn",
  high: "bad",
};

interface Draft {
  name: string;
  project_id: string;
  hazard_level: HazardLevel;
  danger: boolean;
  active: boolean;
  description: string;
}

const EMPTY_DRAFT: Draft = {
  name: "",
  project_id: "",
  hazard_level: "medium",
  danger: false,
  active: true,
  description: "",
};

function draftOf(zone: Zone): Draft {
  return {
    name: zone.name,
    project_id: zone.project_id ?? "",
    hazard_level: zone.hazard_level ?? "medium",
    danger: zone.danger,
    active: zone.active,
    description: zone.description ?? "",
  };
}

function payloadOf(draft: Draft): ZoneCreate {
  return {
    name: draft.name.trim(),
    project_id: draft.project_id.trim() || undefined,
    hazard_level: draft.hazard_level,
    danger: draft.danger,
    active: draft.active,
    description: draft.description.trim() || undefined,
  };
}

export default function ZonesPage() {
  const [create, setCreate] = useState<Draft>(EMPTY_DRAFT);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [edit, setEdit] = useState<Draft>(EMPTY_DRAFT);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<Record<string, string>>({});

  const live = useLiveFeed({ topics: ["zone"], onFrame: () => void refresh() });

  const { data, error, loading, refresh } = usePoll(
    () => api.zones(),
    live.connected ? 30000 : 10000,
    [live.connected],
  );

  const zones = data?.zones ?? [];

  const {
    data: occData,
    error: occError,
    loading: occLoading,
    refresh: refreshOcc,
  } = usePoll(() => api.zoneOccupancy(), 15000);
  const occupancy = occData?.zones ?? [];

  const submitCreate = async () => {
    if (!create.name.trim()) {
      setCreateError("A zone needs a name.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      await api.createZone(payloadOf(create));
      setCreate(EMPTY_DRAFT);
      await refresh();
    } catch (e) {
      setCreateError(errorMessage(e));
    } finally {
      setCreating(false);
    }
  };

  const startEdit = (zone: Zone) => {
    setEditingId(zone.zone_id);
    setEdit(draftOf(zone));
    setConfirmId(null);
    setRowError((prev) => ({ ...prev, [zone.zone_id]: "" }));
  };

  const saveEdit = async (zone: Zone) => {
    if (!edit.name.trim()) {
      setRowError((prev) => ({ ...prev, [zone.zone_id]: "A zone needs a name." }));
      return;
    }
    setBusyId(zone.zone_id);
    try {
      await api.updateZone(zone.zone_id, payloadOf(edit));
      setEditingId(null);
      setRowError((prev) => ({ ...prev, [zone.zone_id]: "" }));
      await refresh();
    } catch (e) {
      setRowError((prev) => ({ ...prev, [zone.zone_id]: errorMessage(e) }));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (zone: Zone) => {
    setBusyId(zone.zone_id);
    try {
      await api.deleteZone(zone.zone_id);
      setConfirmId(null);
      await refresh();
    } catch (e) {
      setRowError((prev) => ({ ...prev, [zone.zone_id]: errorMessage(e) }));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Zones"
        subtitle="Named site areas · hazard level drives severity, the danger flag arms proximity rules"
        action={<LiveChip connected={live.connected} />}
      />

      <SectionTitle>Occupancy &amp; risk</SectionTitle>
      <p className="mb-3 -mt-1 text-[12px] text-txt-3">
        Who is checked into each zone right now, and which zones are generating the most
        warnings — ranked worst-first.
      </p>
      {occError && !occData ? (
        <ErrorState message={occError} onRetry={() => void refreshOcc()} />
      ) : occLoading && !occData ? (
        <Card className="mb-6">
          <Loading label="Loading occupancy…" />
        </Card>
      ) : (
        <Card className="mb-6 overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead>
              <tr className="border-b border-line text-left">
                <Th>Rank</Th>
                <Th>Zone</Th>
                <Th>Workers present</Th>
                <Th>Warnings (outstanding / today / total)</Th>
                <Th className="text-right">Risk score</Th>
              </tr>
            </thead>
            <tbody>
              {occupancy.length ? (
                occupancy.map((row: ZoneOccupancy) => (
                  <tr key={row.zone_id} className="border-b border-line-soft last:border-0">
                    <Td>
                      <span
                        className={`inline-flex h-6 w-6 items-center justify-center rounded-full text-[11px] font-bold ${
                          row.risk_rank === 1 && row.risk_score > 0
                            ? "bg-red-500/15 text-red-500"
                            : "bg-panel-2 text-txt-3"
                        }`}
                      >
                        {row.risk_rank}
                      </span>
                    </Td>
                    <Td>
                      <div className="font-semibold">{row.name}</div>
                      {row.danger ? <Badge tone="bad">danger zone</Badge> : null}
                    </Td>
                    <Td>
                      {row.worker_count === 0 ? (
                        <span className="text-txt-3">Empty</span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {row.workers.map((w) => (
                            <span
                              key={w.worker_id}
                              title={`Since ${timeAgo(w.entered_at)}`}
                              className="rounded-full bg-panel-2 px-2 py-0.5 text-[11px] text-txt-2"
                            >
                              {w.display_name ?? w.worker_id}
                            </span>
                          ))}
                        </div>
                      )}
                    </Td>
                    <Td>
                      <span
                        className={
                          row.warnings.outstanding > 0 ? "font-semibold text-amber-500" : "text-txt-2"
                        }
                      >
                        {row.warnings.outstanding}
                      </span>
                      <span className="text-txt-3"> / {row.warnings.today} / {row.warnings.total}</span>
                    </Td>
                    <Td className="text-right font-mono">{row.risk_score.toFixed(1)}</Td>
                  </tr>
                ))
              ) : (
                <tr>
                  <Td colSpan={5}>
                    <Empty>No zones to report on yet.</Empty>
                  </Td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>
      )}

      <SectionTitle>Add a zone</SectionTitle>
      <Card className="mb-6 px-4 py-4">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submitCreate();
          }}
        >
          <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2 lg:grid-cols-4">
            <Field label="Name" htmlFor="zone-name">
              <input
                id="zone-name"
                required
                value={create.name}
                onChange={(e) => setCreate({ ...create, name: e.target.value })}
                placeholder="e.g. scaffold-east"
                className={inputClass}
              />
            </Field>
            <Field label="Project id" htmlFor="zone-project" hint="Optional">
              <input
                id="zone-project"
                value={create.project_id}
                onChange={(e) => setCreate({ ...create, project_id: e.target.value })}
                placeholder="demo-project"
                className={inputClass}
              />
            </Field>
            <Field label="Hazard level" htmlFor="zone-hazard">
              <select
                id="zone-hazard"
                value={create.hazard_level}
                onChange={(e) =>
                  setCreate({ ...create, hazard_level: e.target.value as HazardLevel })
                }
                className={inputClass}
              >
                {HAZARDS.map((h) => (
                  <option key={h} value={h}>
                    {h}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Description" htmlFor="zone-desc" hint="Optional">
              <input
                id="zone-desc"
                value={create.description}
                onChange={(e) => setCreate({ ...create, description: e.target.value })}
                placeholder="What happens in this zone?"
                className={inputClass}
              />
            </Field>
          </div>

          <div className="mt-3.5 flex flex-wrap items-center gap-5">
            <label className="flex items-center gap-2 text-[13px]" htmlFor="zone-danger">
              <input
                id="zone-danger"
                type="checkbox"
                checked={create.danger}
                onChange={(e) => setCreate({ ...create, danger: e.target.checked })}
                className="h-4 w-4 accent-[var(--accent)]"
              />
              Danger zone (arms proximity alerts)
            </label>
            <label className="flex items-center gap-2 text-[13px]" htmlFor="zone-active">
              <input
                id="zone-active"
                type="checkbox"
                checked={create.active}
                onChange={(e) => setCreate({ ...create, active: e.target.checked })}
                className="h-4 w-4 accent-[var(--accent)]"
              />
              Active
            </label>
            <Button type="submit" disabled={creating || !create.name.trim()}>
              {creating ? "Creating…" : "Create zone"}
            </Button>
          </div>

          {createError ? (
            <p className="mt-2.5 text-[12px] text-red-500" role="alert">
              {createError}
            </p>
          ) : null}
        </form>
      </Card>

      <SectionTitle>Configured zones ({zones.length})</SectionTitle>

      {error && !data ? (
        <ErrorState message={error} onRetry={() => void refresh()} />
      ) : loading && !data ? (
        <Card>
          <Loading label="Loading zones…" />
        </Card>
      ) : (
        <>
          {error ? (
            <Note tone="warn" title="Showing cached zones">
              {error}
            </Note>
          ) : null}
          <Card className="overflow-x-auto">
            <table className="w-full min-w-[820px]">
              <thead>
                <tr className="border-b border-line text-left">
                  <Th>Zone</Th>
                  <Th>Hazard</Th>
                  <Th>Danger</Th>
                  <Th>Active</Th>
                  <Th>Description</Th>
                  <Th>Updated</Th>
                  <Th className="text-right">Actions</Th>
                </tr>
              </thead>
              <tbody>
                {zones.length ? (
                  zones.map((zone) => {
                    const editingRow = editingId === zone.zone_id;
                    const busy = busyId === zone.zone_id;
                    return (
                      <tr key={zone.zone_id} className="border-b border-line-soft last:border-0">
                        <Td>
                          {editingRow ? (
                            <input
                              value={edit.name}
                              onChange={(e) => setEdit({ ...edit, name: e.target.value })}
                              aria-label={`Name of zone ${zone.name}`}
                              className={inputClass}
                            />
                          ) : (
                            <>
                              <div className="font-semibold">{zone.name}</div>
                              <div className="font-mono text-[11px] text-txt-3">
                                {zone.project_id ?? "no project"}
                              </div>
                            </>
                          )}
                        </Td>
                        <Td>
                          {editingRow ? (
                            <select
                              value={edit.hazard_level}
                              onChange={(e) =>
                                setEdit({ ...edit, hazard_level: e.target.value as HazardLevel })
                              }
                              aria-label={`Hazard level of zone ${zone.name}`}
                              className={inputClass}
                            >
                              {HAZARDS.map((h) => (
                                <option key={h} value={h}>
                                  {h}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <Badge tone={HAZARD_TONE[zone.hazard_level] ?? "neutral"}>
                              {zone.hazard_level ?? "–"}
                            </Badge>
                          )}
                        </Td>
                        <Td>
                          {editingRow ? (
                            <input
                              type="checkbox"
                              checked={edit.danger}
                              onChange={(e) => setEdit({ ...edit, danger: e.target.checked })}
                              aria-label={`Danger flag for zone ${zone.name}`}
                              className="h-4 w-4 accent-[var(--accent)]"
                            />
                          ) : zone.danger ? (
                            <Badge tone="bad">danger</Badge>
                          ) : (
                            <span className="text-txt-3">–</span>
                          )}
                        </Td>
                        <Td>
                          {editingRow ? (
                            <input
                              type="checkbox"
                              checked={edit.active}
                              onChange={(e) => setEdit({ ...edit, active: e.target.checked })}
                              aria-label={`Active flag for zone ${zone.name}`}
                              className="h-4 w-4 accent-[var(--accent)]"
                            />
                          ) : (
                            <Badge tone={zone.active ? "good" : "neutral"}>
                              {zone.active ? "on" : "off"}
                            </Badge>
                          )}
                        </Td>
                        <Td className="max-w-[260px]">
                          {editingRow ? (
                            <input
                              value={edit.description}
                              onChange={(e) => setEdit({ ...edit, description: e.target.value })}
                              aria-label={`Description of zone ${zone.name}`}
                              className={inputClass}
                            />
                          ) : (
                            <span className="text-txt-2">{zone.description || "–"}</span>
                          )}
                          {rowError[zone.zone_id] ? (
                            <p className="mt-1 text-[11px] text-red-500" role="alert">
                              {rowError[zone.zone_id]}
                            </p>
                          ) : null}
                        </Td>
                        <Td>
                          <span className="text-[11px] text-txt-3">
                            {fmtDateTime(zone.updated_at)}
                          </span>
                        </Td>
                        <Td className="text-right">
                          <div className="flex flex-wrap justify-end gap-1.5">
                            {editingRow ? (
                              <>
                                <Button
                                  size="sm"
                                  onClick={() => void saveEdit(zone)}
                                  disabled={busy}
                                >
                                  {busy ? "Saving…" : "Save"}
                                </Button>
                                <Button
                                  size="sm"
                                  tone="secondary"
                                  onClick={() => setEditingId(null)}
                                  disabled={busy}
                                >
                                  Cancel
                                </Button>
                              </>
                            ) : confirmId === zone.zone_id ? (
                              <>
                                <Button
                                  size="sm"
                                  tone="bad"
                                  onClick={() => void remove(zone)}
                                  disabled={busy}
                                  ariaLabel={`Confirm deleting zone ${zone.name}`}
                                >
                                  {busy ? "Deleting…" : "Confirm delete"}
                                </Button>
                                <Button
                                  size="sm"
                                  tone="secondary"
                                  onClick={() => setConfirmId(null)}
                                  disabled={busy}
                                >
                                  Keep
                                </Button>
                              </>
                            ) : (
                              <>
                                <Button
                                  size="sm"
                                  tone="secondary"
                                  onClick={() => startEdit(zone)}
                                  ariaLabel={`Edit zone ${zone.name}`}
                                >
                                  Edit
                                </Button>
                                <Button
                                  size="sm"
                                  tone="bad"
                                  onClick={() => setConfirmId(zone.zone_id)}
                                  ariaLabel={`Delete zone ${zone.name}`}
                                >
                                  Delete
                                </Button>
                              </>
                            )}
                          </div>
                        </Td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <Td colSpan={7}>
                      <Empty>
                        No zones configured yet — add one above so alerts can be scoped to an area.
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
