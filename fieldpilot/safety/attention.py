"""Attention / cognitive-engagement state machine.

Verifies whether a worker actually *noticed* a hazard, rather than merely being near it. A hazard
target starts PASSIVE; the worker must hold their gaze on it for a contiguous `dwell_ms` window to be
counted as NOTICED (a brief glance under `glance_ms` does not count). If the hazard persists
unnoticed past `unnoticed_after_ms` it becomes UNNOTICED, and past `escalate_after_ms` it ESCALATES.

The state machine is pure: it takes explicit `(now_ms, gaze_on_hazard)` inputs so it can be unit
tested deterministically. Turning head-pose into the `gaze_on_hazard` boolean lives in
`fieldpilot.perspective`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fieldpilot.core.types import (
    AttentionState,
    FrameResult,
    HazardEvent,
    HazardType,
    Severity,
)


@dataclass
class HazardAttention:
    """Tracks one worker's engagement with one hazard target."""

    hazard_id: str
    onset_ms: float
    dwell_ms: float
    glance_ms: float
    unnoticed_after_ms: float
    escalate_after_ms: float
    state: AttentionState = AttentionState.PASSIVE
    _dwell_start: float | None = field(default=None, repr=False)
    _last_seen_ms: float = 0.0

    def update(self, now_ms: float, gaze_on_hazard: bool) -> AttentionState:
        self._last_seen_ms = now_ms

        if gaze_on_hazard:
            if self._dwell_start is None:
                self._dwell_start = now_ms
            contiguous = now_ms - self._dwell_start
            # A glance is too short to count; only a sustained dwell confirms engagement.
            if contiguous >= self.dwell_ms:
                self.state = AttentionState.NOTICED
        else:
            self._dwell_start = None

        if self.state is AttentionState.NOTICED:
            return self.state  # engagement confirmed; terminal.

        elapsed = now_ms - self.onset_ms
        if elapsed >= self.escalate_after_ms:
            self.state = AttentionState.ESCALATED
        elif elapsed >= self.unnoticed_after_ms:
            self.state = AttentionState.UNNOTICED
        return self.state


class AttentionTracker:
    """Manages a HazardAttention per (worker, hazard) pair and emits transition events."""

    def __init__(self, cfg):
        att = cfg.section("attention")
        self.dwell_ms = float(att.get("dwell_ms", 600))
        self.glance_ms = float(att.get("glance_ms", 200))
        self.unnoticed_after_ms = float(att.get("unnoticed_after_ms", 2500))
        self.escalate_after_ms = float(att.get("escalate_after_ms", 6000))
        self._machines: dict[str, HazardAttention] = {}
        self._emitted: dict[str, AttentionState] = {}

    def _key(self, worker_id: int, hazard_id: str) -> str:
        return f"{worker_id}:{hazard_id}"

    def observe(
        self,
        result: FrameResult,
        hazards: list[HazardEvent],
        gaze_fn,
    ) -> list[HazardEvent]:
        """For every active hazard, check each *other* worker's engagement with it.

        `gaze_fn(person, bbox) -> bool` decides if a worker is currently looking at the hazard.
        Returns new UNNOTICED / ESCALATED events on state transitions.
        """

        now_ms = result.frame.ts_monotonic * 1000.0
        out: list[HazardEvent] = []
        active_keys: set[str] = set()

        for hazard in hazards:
            if hazard.bbox is None:
                continue
            for person in result.persons:
                if person.track_id == hazard.track_id:
                    continue  # the worker who *is* the hazard (e.g. the faller) is not the observer
                key = self._key(person.track_id, hazard.id)
                active_keys.add(key)
                machine = self._machines.get(key)
                if machine is None:
                    machine = HazardAttention(
                        hazard_id=hazard.id,
                        onset_ms=now_ms,
                        dwell_ms=self.dwell_ms,
                        glance_ms=self.glance_ms,
                        unnoticed_after_ms=self.unnoticed_after_ms,
                        escalate_after_ms=self.escalate_after_ms,
                    )
                    self._machines[key] = machine

                looking = bool(gaze_fn(person, hazard.bbox))
                state = machine.update(now_ms, looking)

                prev = self._emitted.get(key)
                if state != prev and state in (AttentionState.UNNOTICED, AttentionState.ESCALATED):
                    self._emitted[key] = state
                    sev = Severity.HIGH if state is AttentionState.ESCALATED else Severity.MEDIUM
                    verb = "has not acknowledged" if state is AttentionState.UNNOTICED else "still ignoring"
                    out.append(
                        HazardEvent(
                            hazard_type=HazardType.UNNOTICED_HAZARD,
                            severity=sev,
                            message=f"Worker {person.track_id} {verb} a {hazard.category()} hazard.",
                            frame_index=result.frame.index,
                            ts_monotonic=result.frame.ts_monotonic,
                            track_id=person.track_id,
                            bbox=hazard.bbox,
                            meta={
                                "attention_state": state.value,
                                "source_hazard": hazard.id,
                                "source_type": hazard.category(),
                            },
                        )
                    )
                elif state is AttentionState.NOTICED and prev is not AttentionState.NOTICED:
                    self._emitted[key] = state  # record engagement so we never re-flag it

        # drop machines for hazards that are no longer active.
        for key in list(self._machines.keys()):
            if key not in active_keys:
                self._machines.pop(key, None)
                self._emitted.pop(key, None)
        return out
