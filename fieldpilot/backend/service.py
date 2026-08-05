"""Backend orchestrator — the wiring that enforces the platform data flow.

    Model → Event → [persist] → TriggerEngine → [alert] → RuleEngine → actions
                    (PostgreSQL/SQLite)                        ↓
                                       Notification / RFI / Inspection / Dashboard

Subscribes to `events.*` on the bus. For every event: persist it, run it through the
trigger engine (dedup/merge/track/resolve), build context, evaluate rules, and execute
the resulting actions. Models never reach past the bus.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fieldpilot.alerts.speech import spoken_phrase
from fieldpilot.backend.store import PlatformStore
from fieldpilot.broadcast import BroadcastHub
from fieldpilot.events.bus import EventBus
from fieldpilot.events.schema import Event
from fieldpilot.events.store import EventRepository
from fieldpilot.llm.verifier import LLMVerifier, Verdict
from fieldpilot.logging_.logger import get_logger
from fieldpilot.notifications.service import NotificationService
from fieldpilot.reasoning.rfi import RFIDrafter
from fieldpilot.rules.engine import RuleAction, RuleEngine
from fieldpilot.triggers.engine import Alert, TriggerEngine
from fieldpilot.zones import ZoneService

log = get_logger("fieldpilot.backend.service")


class Orchestrator:
    def __init__(
        self,
        *,
        bus: EventBus,
        events: EventRepository,
        store: PlatformStore,
        triggers: TriggerEngine,
        rules: RuleEngine,
        notifications: NotificationService,
        verifier: LLMVerifier | None = None,
        hub: BroadcastHub | None = None,
        zones: ZoneService | None = None,
        rfi_drafter: RFIDrafter | None = None,
        project_id: str = "default",
        suppress_max_severity: str = "medium",
    ) -> None:
        self.bus = bus
        self.events = events
        self.store = store
        self.triggers = triggers
        self.rules = rules
        self.notifications = notifications
        self.verifier = verifier
        self.hub = hub
        self.zones = zones
        self.rfi_drafter = rfi_drafter
        self.project_id = project_id
        self.suppress_max_severity = suppress_max_severity
        self._started = False

    #: severity ordering used by the LLM suppression ceiling
    _SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    def _may_suppress(self, severity: str) -> bool:
        """True when the LLM is permitted to bin an alert of this severity on its own.

        Two guards, both learned from measured behaviour of llama3.2:3b on this pipeline:

        1. **No vision, no veto.** A metadata-only verifier cannot assess imagery, yet a small
           model still rejects with "no clear visual evidence in bounding box" — 14 of 19
           observed rejections cited a frame the model was never given. Without vision the LLM
           may annotate, never suppress.
        2. **Severity ceiling.** Even with vision, it must not bin a serious hazard alone.
        """

        if self.verifier is not None and not getattr(self.verifier, "vision", False):
            return False
        ceiling = self._SEVERITY_RANK.get(self.suppress_max_severity, 1)
        return self._SEVERITY_RANK.get(str(severity).lower(), 1) <= ceiling

    async def start(self) -> None:
        if self._started:
            return
        await self.bus.subscribe("events.*", self._on_event_raw)
        self._started = True
        chain = "bus → triggers → LLM → rules → actions" if self.verifier else \
            "bus → triggers → rules → actions"
        log.info("orchestrator started — %s", chain)

    # -- pipeline --------------------------------------------------------------

    async def _on_event_raw(self, topic: str, message: dict[str, Any]) -> None:
        if topic == "events.all":  # avoid double-processing the fan-out copy
            return
        event = Event(**message)
        await self.handle_event(event)

    async def handle_event(self, event: Event) -> None:
        # 1. durable record first — nothing is ever lost, even if downstream fails.
        await self.events.save_event(event)

        # 2. intelligent filtering: dedup / merge / track / auto-resolve.
        result = await self.triggers.process(event)

        # 3. only brand-new / reactivated alerts hop through the LLM + rules.
        if result.outcome not in ("created", "reactivated") or result.alert is None:
            return

        alert = result.alert

        # 4. LLM verifier gate — noise control, NOT a safety authority.
        #
        # A small local model does reject genuine hazards: llama3.2:3b was observed suppressing a
        # 0.97-confidence fall ("worker collapsed and is motionless") as "low detector
        # confidence". Letting it silently bin a critical alert fails in the dangerous direction
        # and contradicts this system's advisory, fail-safe posture. So the LLM may only suppress
        # up to `suppress_max_severity`; above that its doubt is *recorded on the alert* and the
        # alert still reaches people, flagged as disputed for a human to judge.
        if self.verifier is not None:
            verdict = await self.verifier.verify(alert.to_dict())
            record = verdict.to_dict()
            disputed = not verdict.confirmed and not self._may_suppress(alert.severity)
            # `disputed` goes inside the verdict so `set_verdict` persists it with everything
            # else; a flag set only on the in-memory alert would show up in the live push and
            # then vanish on the next page load.
            record["disputed"] = disputed
            await self.triggers.set_verdict(alert.dedup_key, record)
            alert.payload["llm_verdict"] = record
            if not verdict.confirmed:
                if not disputed:
                    log.info("alert %s REJECTED by LLM: %s",
                             alert.alert_id, verdict.reasoning[:100])
                    await self.triggers.suppress(alert.dedup_key)
                    await self._notify_rejected(alert, verdict)
                    return
                alert.payload["llm_disputed"] = True
                reason = ("the verifier has no vision and cannot judge imagery"
                          if not getattr(self.verifier, "vision", False)
                          else f"severity exceeds the ceiling {self.suppress_max_severity!r}")
                log.warning(
                    "alert %s (%s) was rejected by the LLM but is ESCALATED ANYWAY — %s. "
                    "LLM said: %s",
                    alert.alert_id, alert.severity, reason, verdict.reasoning[:100],
                )
            else:
                log.info("alert %s CONFIRMED by LLM (conf=%.2f)",
                         alert.alert_id, verdict.confidence)

        # 5. rules + baseline notification — only confirmed alerts reach people.
        context = await self._build_context(event)
        actions = self.rules.evaluate(event, context)
        for action in actions:
            await self._execute(action, event, alert)
        await self._notify_new(alert)

        # 6. push to open dashboards, and advise the *other* workers in this zone (PRD §4.4).
        # Full alerts go to supervisory dashboards; worker devices get only the downgraded
        # zone-scoped advisory, so a device is never a firehose of site-wide alert traffic.
        if self.hub is not None:
            record = alert.to_dict()
            # Each audience hears the same fact phrased for them (see alerts/speech.py). The
            # phrase travels as data because the client — a browser or the worker's phone — is the
            # machine with the speaker; the server's own `tts.py` would speak into an empty room.
            await self.hub.publish(
                "alert",
                {**record, "speech": spoken_phrase(record, audience="dashboard")},
                zone=alert.zone,
                audience="dashboard",
            )
            # The worker at risk hears the primary, second-person verdict on their own device...
            spoke_to_worker = await self.hub.alert_worker(record)
            # ...and everyone *else* in the zone gets only the downgraded advisory, so the subject
            # is not told twice about their own hazard.
            await self.hub.advise_zone(
                record, exclude_worker=alert.worker_id if spoke_to_worker else None
            )

    async def _notify_new(self, alert: Alert) -> None:
        """Baseline dashboard notification for a confirmed NEW alert."""

        await self.notifications.notify(
            dedup_key=f"alert:{alert.alert_id}",
            subject=f"[{alert.severity.upper()}] {alert.event_type}: {alert.message or ''}",
            body=str(alert.message or ""),
            severity=alert.severity,
            channels=["dashboard"],
            alert_id=alert.alert_id,
            meta={"alert": alert.to_dict()},
        )

    async def _notify_rejected(self, alert: Alert, verdict: Verdict) -> None:
        """Tell the dashboard the LLM rejected this alert (suppressed)."""

        await self.notifications.notify(
            dedup_key=f"rejected:{alert.alert_id}",
            subject=f"[REJECTED by LLM] {alert.event_type}: {alert.message or ''}",
            body=str(verdict.reasoning)[:280],
            severity="low",
            channels=["dashboard"],
            alert_id=alert.alert_id,
            meta={"alert": alert.to_dict(), "verdict": verdict.to_dict()},
        )

    async def _build_context(self, event: Event) -> dict[str, Any]:
        """Context available to rule conditions (e.g. is the worker in a danger zone)."""

        ctx: dict[str, Any] = {"in_danger_zone": False}
        # A zone flagged `danger` in the registry makes this a property of the site, not merely
        # of a proximity alert happening to be open at the same moment.
        if self.zones is not None and self.zones.is_danger_zone(event.zone):
            ctx["in_danger_zone"] = True
        if self.zones is not None:
            ctx["zone_hazard_level"] = self.zones.hazard_level(event.zone)
        if not ctx["in_danger_zone"] and event.worker_id:
            for alert in await self.triggers.list_tracked():
                if (
                    alert.worker_id == event.worker_id
                    and alert.event_type == "proximity"
                    and alert.state in ("NEW", "ACTIVE")
                ):
                    ctx["in_danger_zone"] = True
                    break
        return ctx

    async def _draft_rfi(
        self, action: RuleAction, event: Event, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Draft an RFI grounded in the retrieved clause, or fall back to a plain record.

        A drafting failure must still leave an RFI on the review queue — losing the deviation
        entirely because the LLM or Qdrant was down would be the worse outcome.
        """

        message = str(params.get("message") or "")
        priority = str(params.get("priority", "normal"))
        if self.rfi_drafter is not None:
            try:
                drafted = await self.rfi_drafter.draft(
                    event=event.model_dump_json_safe(),
                    message=message,
                    priority=priority,
                    project_id=self.project_id,
                )
                return drafted.to_record(extra={
                    "rule": action.rule_name, "image_url": event.image_url,
                })
            except Exception:  # noqa: BLE001 — never drop the deviation
                log.exception("RFI drafting failed — filing an ungrounded RFI instead")
        return {
            "rfi_id": uuid.uuid4().hex,
            "event_id": event.event_id,
            "title": (message or f"RFI from rule {action.rule_name}")[:200],
            "summary": message,
            "body": message,
            "priority": priority,
            "zone": event.zone,
            "status": "pending_review",
            "citation": None,
            "created_at": time.time(),
            "payload": {
                "rule": action.rule_name,
                "event": event.model_dump_json_safe(),
                "image_url": event.image_url,
                "grounded": False,
                "citations": [],
                "llm_used": False,
            },
        }

    # -- action execution ---------------------------------------------------------

    async def _execute(self, action: RuleAction, event: Event, alert: Alert | None) -> None:
        atype = action.action_type
        p = action.params
        if atype == "create_alert":
            await self.notifications.notify(
                dedup_key=f"rule:{action.rule_id}:{event.worker_id or event.camera_id}",
                subject=str(p.get("message") or f"Rule {action.rule_name} fired"),
                body=str(p.get("message") or ""),
                severity=str(p.get("severity") or (alert.severity if alert else "high")),
                alert_id=alert.alert_id if alert else None,
                meta={"rule": action.rule_name, "event": event.model_dump_json_safe()},
            )
        elif atype == "generate_rfi":
            record = await self._draft_rfi(action, event, p)
            await self.store.save_rfi(record)
            if self.hub is not None:
                await self.hub.publish("rfi", record, zone=event.zone)
            log.info("RFI %s filed for review (grounded=%s) by rule %s",
                     record["rfi_id"], record["payload"].get("grounded"), action.rule_name)
        elif atype == "request_inspection":
            record = {
                "inspection_id": uuid.uuid4().hex,
                "event_id": event.event_id,
                "priority": str(p.get("priority", "normal")),
                "zone": event.zone,
                "message": str(p.get("message") or f"Inspection requested by {action.rule_name}"),
                "status": "requested",
                "created_at": time.time(),
            }
            await self.store.save_inspection(record)
            if self.hub is not None:
                await self.hub.publish("inspection", record, zone=event.zone)
            log.info("inspection requested by rule %s for event %s", action.rule_name, event.event_id)
        elif atype == "notify":
            channels = p.get("channels")
            if isinstance(channels, str):
                channels = [c.strip() for c in channels.split(",") if c.strip()]
            await self.notifications.notify(
                dedup_key=f"rule:{action.rule_id}:{event.worker_id or event.camera_id}",
                subject=str(p.get("message") or f"Rule {action.rule_name} fired"),
                body=str(p.get("message") or ""),
                severity=str(p.get("severity", "medium")),
                channels=channels,
                alert_id=alert.alert_id if alert else None,
                meta={"rule": action.rule_name},
            )
        else:
            log.warning("unknown rule action type %r — ignored", atype)
