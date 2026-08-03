"""FieldPilot backend — FastAPI application.

Exposes the event-driven platform over REST:

    POST /events                     ingest a canonical event (models / edge devices)
    GET  /events                     query the durable event log
    GET  /alerts                     filterable alert board (state/severity/zone/worker/type)
    POST /alerts/{id}/resolve        operator actions
    GET/POST/PUT/DELETE /rules       configurable rules engine
    GET  /workers/{id}/timeline      worker 360° view
    GET  /notifications /rfis /inspections
    GET/POST/PUT/DELETE /zones       site zone registry
    POST /alerts/{id}/feedback       supervisor approve/reject → learning loop
    POST /learning/train             fine-tune + mAP50 delta gate
    GET  /blueprints                 RAG corpus status; POST /blueprints/{ingest,search}
    POST /rfis/{id}/{approve,reject} RFI review queue
    WS   /ws                         live push + zone-scoped cross-worker advisories

    uv run python -m fieldpilot.backend.app          # http://localhost:8100
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fieldpilot.backend.service import Orchestrator
from fieldpilot.backend.store import create_platform_store
from fieldpilot.broadcast import BroadcastHub
from fieldpilot.core.config import Config, load_config
from fieldpilot.events.bus import create_bus, publish_event
from fieldpilot.events.schema import Event
from fieldpilot.events.store import create_repository
from fieldpilot.feedback import FEEDBACK_TABLE, FeedbackService
from fieldpilot.learning import LEARNING_RUNS_TABLE, LearningService
from fieldpilot.llm.verifier import LLMVerifier
from fieldpilot.logging_.logger import get_logger, setup_logging
from fieldpilot.notifications.service import TOPIC_DASHBOARD, NotificationService
from fieldpilot.reasoning import BlueprintIndex, Embedder, RFIDrafter, ingest_directory
from fieldpilot.reasoning.ingest import SUPPORTED, parse_metadata
from fieldpilot.rules.engine import Rule, RuleEngine, default_rules
from fieldpilot.storage import DocStore
from fieldpilot.triggers.cache import create_cache
from fieldpilot.triggers.engine import TriggerEngine
from fieldpilot.zones import ZONES_TABLE, ZoneService

log = get_logger("fieldpilot.backend")


class RuleIn(BaseModel):
    name: str
    enabled: bool = True
    priority: int = 100
    event_types: list[str] = []
    conditions: list[dict[str, Any]] = []
    action: dict[str, Any] = {}
    cooldown_s: float = 0.0


class InspectionControlIn(BaseModel):
    enabled: bool


class ZoneIn(BaseModel):
    name: str | None = None
    project_id: str | None = None
    hazard_level: str | None = None
    danger: bool | None = None
    active: bool | None = None
    description: str | None = None


class FeedbackIn(BaseModel):
    decision: str
    label: str | None = None
    notes: str = ""
    reviewer: str = "supervisor"
    bbox: list[float] | None = None


class TrainIn(BaseModel):
    epochs: int | None = None
    base_weights: str | None = None


class ReviewIn(BaseModel):
    reviewer: str = "supervisor"
    notes: str = ""


class IngestIn(BaseModel):
    replace: bool = False


class SearchIn(BaseModel):
    query: str
    project_id: str | None = None
    zone: str | None = None
    category: str | None = None
    top_k: int = 5


def _sev_penalty(severity: str) -> int:
    return {"critical": 25, "high": 15, "medium": 8, "low": 3}.get(severity, 5)


def _ppe_weights_status(cfg: Config) -> dict[str, Any]:
    model = cfg.get("detection.ppe_model")
    if not model:
        return {"enabled": False, "model": None,
                "reason": "detection.ppe_model is not configured"}
    if not Path(str(model)).is_file():
        return {"enabled": False, "model": str(model),
                "reason": f"weights missing at {model} — run `make fetch-models`"}
    return {"enabled": True, "model": str(model), "reason": None}


def create_app(cfg: Config) -> FastAPI:
    backend = cfg.get("events.backend", "sqlite")
    database_url = cfg.get("events.database_url", "data/platform.db")
    bus_backend = cfg.get("events.bus_backend", "memory")
    redis_url = cfg.get("events.redis_url", "redis://localhost:6379/0")

    bus = create_bus(bus_backend, redis_url)
    cache = create_cache(bus_backend if bus_backend == "redis" else "memory", redis_url)
    events_repo = create_repository(backend, cfg.get("events.events_db_url", "data/events.db"))
    store = create_platform_store(backend, database_url)
    triggers = TriggerEngine(
        cache,
        bus,
        dedup_window_s=float(cfg.get("triggers.dedup_window_s", 45)),
        resolve_after_s=float(cfg.get("triggers.resolve_after_s", 90)),
        cache_ttl_s=int(cfg.get("triggers.cache_ttl_s", 6 * 3600)),
        alert_sink=lambda a: store.upsert_alert(a),
    )
    rules_engine = RuleEngine()
    notifications = NotificationService(
        store, cache, bus,
        dedup_window_s=float(cfg.get("notifications.dedup_window_s", 300)),
    )
    llm_cfg = cfg.get("llm", {}) or {}
    verifier = None
    if bool(llm_cfg.get("enabled", False)):
        verifier = LLMVerifier(
            ollama_host=str(llm_cfg.get("ollama_host", cfg.get("reasoning.ollama_host",
                          "http://localhost:11434"))),
            model=str(llm_cfg.get("model", "llama3.2:3b")),
            vision=bool(llm_cfg.get("vision", False)),
            enabled=True,
            timeout_s=float(llm_cfg.get("timeout_s", 12.0)),
        )
        log.info("LLM verifier enabled: model=%s vision=%s",
                 verifier.model, verifier.vision)

    # --- zones, feedback, learning: three tables on one shared doc store -------------
    docs = DocStore(backend, database_url)
    zones = ZoneService(docs)
    feedback = FeedbackService(docs)
    learning = LearningService(
        docs, feedback,
        base_weights=str(cfg.get("learning.base_weights", "models/ppe_css.pt")),
        val_set=str(cfg.get("learning.val_set", "data/val_set")),
        output_dir=str(cfg.get("learning.output_dir", "models/finetuned")),
        epochs=int(cfg.get("learning.epochs", 20)),
        promote_if_delta_gte=float(cfg.get("learning.promote_if_delta_gte", 0.0)),
        min_samples=int(cfg.get("learning.min_samples", 8)),
    )

    # --- RAG: blueprint retrieval + RFI drafting -------------------------------------
    project_id = str(cfg.get("reasoning.project_id", "default"))
    ollama_host = str(cfg.get("reasoning.ollama_host", "http://localhost:11434"))
    embedder = Embedder(
        ollama_host=ollama_host,
        model=str(cfg.get("reasoning.embed_model", "nomic-embed-text")),
    )
    blueprints = BlueprintIndex(
        embedder,
        url=str(cfg.get("reasoning.qdrant_url", "http://localhost:6333")),
        collection=str(cfg.get("reasoning.qdrant_collection", "blueprints")),
        top_k=int(cfg.get("reasoning.top_k", 5)),
    )
    rfi_drafter = RFIDrafter(
        blueprints,
        ollama_host=ollama_host,
        model=str(cfg.get("reasoning.llm_model", "llama3.2:3b")),
    )
    hub = BroadcastHub(bus)

    orchestrator = Orchestrator(
        bus=bus, events=events_repo, store=store,
        triggers=triggers, rules=rules_engine, notifications=notifications,
        verifier=verifier, hub=hub, zones=zones, rfi_drafter=rfi_drafter,
        project_id=project_id,
        suppress_max_severity=str(cfg.get("llm.suppress_max_severity", "medium")),
    )
    blueprints_dir = str(cfg.get("reasoning.blueprints_dir", "data/blueprints"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await bus.start()
        await events_repo.start()
        await store.start()
        await docs.start([ZONES_TABLE, FEEDBACK_TABLE, LEARNING_RUNS_TABLE])
        await zones.start()

        stored_rules = await store.list_rules()
        if not stored_rules:
            for r in default_rules():
                await store.put_rule(r.to_dict())
            stored_rules = await store.list_rules()
            log.info("seeded %d default rules", len(stored_rules))
        rules_engine.replace_rules([Rule.from_dict(r) for r in stored_rules])

        await orchestrator.start()
        await hub.start()
        # dashboard notifications already land on the bus — forward them to open browsers
        # instead of sprinkling hub.publish() through the notification paths.
        async def _forward_notification(_topic: str, note: dict[str, Any]) -> None:
            await hub.publish("notification", note, audience="dashboard")

        await bus.subscribe(TOPIC_DASHBOARD, _forward_notification)

        if not await blueprints.start():
            log.warning("blueprint index unavailable — RFIs will be filed ungrounded")
        triggers.start_sweeper(interval_s=float(cfg.get("triggers.sweep_interval_s", 5)))
        log.info("FieldPilot backend up — bus=%s store=%s", bus_backend, backend)
        try:
            yield
        finally:
            await triggers.stop()
            await blueprints.stop()
            await bus.stop()
            await events_repo.stop()
            await store.stop()
            await docs.stop()

    app = FastAPI(title="FieldPilot AI — Backend", version="0.2.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # serve captured alert snapshots (the annotated bbox JPEGs the edge writes)
    import os

    os.makedirs("data/alerts", exist_ok=True)
    app.mount("/images", StaticFiles(directory="data/alerts"), name="alert-images")

    # ---------------------------------------------------------------- events

    @app.post("/events", status_code=202)
    async def ingest_event(event: Event) -> dict[str, str]:
        """Models/edge devices push canonical events here → straight onto the bus."""

        await publish_event(bus, event)
        return {"event_id": event.event_id, "status": "accepted"}

    @app.get("/events")
    async def list_events(
        event_type: str | None = None,
        worker_id: str | None = None,
        zone: str | None = None,
        since: float | None = None,
        limit: int = Query(default=200, le=1000),
    ):
        return {"events": await events_repo.list_events(
            event_type=event_type, worker_id=worker_id, zone=zone, since=since, limit=limit)}

    @app.get("/events/stats")
    async def event_stats(since: float | None = None):
        return {"counts_by_type": await events_repo.count_by_type(since)}

    # ---------------------------------------------------------------- alerts

    @app.get("/alerts")
    async def list_alerts(
        state: str | None = None,
        severity: str | None = None,
        worker_id: str | None = None,
        zone: str | None = None,
        event_type: str | None = None,
        since: float | None = None,
        limit: int = Query(default=200, le=1000),
    ):
        return {"alerts": await store.list_alerts(
            state=state, severity=severity, worker_id=worker_id, zone=zone,
            event_type=event_type, since=since, limit=limit)}

    @app.get("/alerts/{alert_id}")
    async def get_alert(alert_id: str):
        alert = await store.get_alert(alert_id)
        if alert is None:
            raise HTTPException(404, "alert not found")
        return alert

    async def _mutate(alert_id: str, op: str) -> dict[str, Any]:
        alert = await store.get_alert(alert_id)
        if alert is None:
            raise HTTPException(404, "alert not found")
        if op == "resolve":
            await triggers.resolve(alert["dedup_key"])
        elif op == "suppress":
            await triggers.suppress(alert["dedup_key"])
        elif op == "unsuppress":
            await triggers.unsuppress(alert["dedup_key"])
        updated = await store.get_alert(alert_id)
        return {"alert": updated}

    @app.post("/alerts/{alert_id}/resolve")
    async def resolve_alert(alert_id: str):
        return await _mutate(alert_id, "resolve")

    @app.post("/alerts/{alert_id}/suppress")
    async def suppress_alert(alert_id: str):
        return await _mutate(alert_id, "suppress")

    @app.post("/alerts/{alert_id}/unsuppress")
    async def unsuppress_alert(alert_id: str):
        return await _mutate(alert_id, "unsuppress")

    # ---------------------------------------------------------------- rules

    @app.get("/rules")
    async def list_rules():
        return {"rules": [r.to_dict() for r in rules_engine.list_rules()]}

    @app.post("/rules", status_code=201)
    async def create_rule(rule_in: RuleIn):
        rule = Rule.from_dict({**rule_in.model_dump(), "rule_id": uuid.uuid4().hex})
        await store.put_rule(rule.to_dict())
        rules_engine.add_rule(rule)
        return rule.to_dict()

    @app.get("/rules/{rule_id}")
    async def get_rule(rule_id: str):
        rule = rules_engine.get_rule(rule_id)
        if rule is None:
            raise HTTPException(404, "rule not found")
        return rule.to_dict()

    @app.put("/rules/{rule_id}")
    async def update_rule(rule_id: str, rule_in: RuleIn):
        if rules_engine.get_rule(rule_id) is None:
            raise HTTPException(404, "rule not found")
        rule = Rule.from_dict({**rule_in.model_dump(), "rule_id": rule_id})
        await store.put_rule(rule.to_dict())
        rules_engine.add_rule(rule)
        return rule.to_dict()

    @app.delete("/rules/{rule_id}")
    async def delete_rule(rule_id: str):
        if not rules_engine.remove_rule(rule_id):
            raise HTTPException(404, "rule not found")
        await store.delete_rule(rule_id)
        return {"deleted": rule_id}

    # ---------------------------------------------------------------- notifications / rfis / inspections

    @app.get("/notifications")
    async def list_notifications(limit: int = Query(default=200, le=1000)):
        return {"notifications": await store.list_notifications(limit=limit)}

    @app.get("/rfis")
    async def list_rfis(
        status: str | None = None, limit: int = Query(default=200, le=1000)
    ):
        return {"rfis": await store.list_rfis(status=status, limit=limit)}

    @app.get("/rfis/{rfi_id}")
    async def get_rfi(rfi_id: str):
        rfi = await store.get_rfi(rfi_id)
        if rfi is None:
            raise HTTPException(404, "RFI not found")
        return rfi

    async def _review_rfi(rfi_id: str, decision: str, body: ReviewIn) -> dict[str, Any]:
        if await store.get_rfi(rfi_id) is None:
            raise HTTPException(404, "RFI not found")
        updated = await store.update_rfi(rfi_id, {
            "status": decision, "reviewer": body.reviewer,
            "notes": body.notes, "reviewed_at": time.time(),
        })
        if updated is not None:
            await hub.publish("rfi", updated, zone=updated.get("zone"))
        return updated or {}

    @app.post("/rfis/{rfi_id}/approve")
    async def approve_rfi(rfi_id: str, body: ReviewIn):
        return await _review_rfi(rfi_id, "approved", body)

    @app.post("/rfis/{rfi_id}/reject")
    async def reject_rfi(rfi_id: str, body: ReviewIn):
        return await _review_rfi(rfi_id, "rejected", body)

    @app.get("/inspections")
    async def list_inspections(
        status: str | None = None, limit: int = Query(default=200, le=1000)
    ):
        return {"inspections": await store.list_inspections(status=status, limit=limit)}

    @app.post("/inspections/{inspection_id}/complete")
    async def complete_inspection(inspection_id: str, body: ReviewIn):
        if await store.get_inspection(inspection_id) is None:
            raise HTTPException(404, "inspection not found")
        updated = await store.update_inspection(inspection_id, {
            "status": "completed", "notes": body.notes, "completed_at": time.time(),
        })
        if updated is not None:
            await hub.publish("inspection", updated, zone=updated.get("zone"))
        return updated or {}

    # ---------------------------------------------------------------- zones

    @app.get("/zones")
    async def list_zones(project_id: str | None = None, active_only: bool = False):
        return {"zones": await zones.list(project_id=project_id, active_only=active_only)}

    @app.post("/zones", status_code=201)
    async def create_zone(body: ZoneIn):
        try:
            zone = await zones.create(body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await hub.publish("zone", zone)
        return zone

    @app.get("/zones/{zone_id}")
    async def get_zone(zone_id: str):
        zone = await zones.get(zone_id)
        if zone is None:
            raise HTTPException(404, "zone not found")
        return zone

    @app.put("/zones/{zone_id}")
    async def update_zone(zone_id: str, body: ZoneIn):
        try:
            zone = await zones.update(zone_id, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if zone is None:
            raise HTTPException(404, "zone not found")
        await hub.publish("zone", zone)
        return zone

    @app.delete("/zones/{zone_id}")
    async def delete_zone(zone_id: str):
        if not await zones.delete(zone_id):
            raise HTTPException(404, "zone not found")
        return {"deleted": True, "zone_id": zone_id}

    # ---------------------------------------------------------------- feedback

    @app.post("/alerts/{alert_id}/feedback")
    async def submit_feedback(alert_id: str, body: FeedbackIn):
        """Supervisor approves/rejects an alert — the label the learning loop trains on."""

        alert = await store.get_alert(alert_id)
        if alert is None:
            raise HTTPException(404, "alert not found")
        try:
            record = await feedback.record(
                alert=alert, decision=body.decision, label=body.label,
                reviewer=body.reviewer, notes=body.notes, bbox=body.bbox,
            )
        except ValueError as exc:
            raise HTTPException(400, f"invalid decision: {exc}") from exc
        # a rejected detection is also a false positive to suppress right now
        if record["decision"] == "reject":
            await triggers.suppress(alert["dedup_key"])
        return record

    @app.get("/feedback")
    async def list_feedback(
        decision: str | None = None,
        event_type: str | None = None,
        alert_id: str | None = None,
        unconsumed_only: bool = False,
        limit: int = Query(default=200, le=1000),
    ):
        return {"feedback": await feedback.list(
            decision=decision, event_type=event_type, alert_id=alert_id,
            unconsumed_only=unconsumed_only, limit=limit)}

    @app.get("/feedback/stats")
    async def feedback_stats():
        return await feedback.stats()

    # ---------------------------------------------------------------- learning

    @app.post("/learning/train")
    async def start_training(body: TrainIn):
        try:
            return await learning.start_run(
                epochs=body.epochs, base_weights=body.base_weights
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/learning/runs")
    async def list_learning_runs(limit: int = Query(default=50, le=500)):
        return {"runs": await learning.list_runs(limit=limit)}

    @app.get("/learning/latest")
    async def latest_learning_run():
        return await learning.latest_delta()

    @app.get("/learning/runs/{run_id}")
    async def get_learning_run(run_id: str):
        run = await learning.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    # ---------------------------------------------------------------- blueprints / RAG

    @app.get("/blueprints")
    async def blueprint_status():
        docs_found = []
        root = Path(blueprints_dir)
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if path.is_file() and path.suffix.lower() in SUPPORTED:
                    meta = parse_metadata(path)
                    docs_found.append({
                        "name": path.name, "project_id": meta["project_id"],
                        "zone": meta["zone"], "category": meta["category"],
                        "size_bytes": path.stat().st_size,
                    })
        return {
            "documents": docs_found,
            "indexed_chunks": await blueprints.count(),
            "embeddings": await embedder.probe(),
            "available": blueprints.available,
        }

    @app.post("/blueprints/ingest")
    async def ingest_blueprints(body: IngestIn):
        if not blueprints.available and not await blueprints.start():
            raise HTTPException(503, "Qdrant unavailable — cannot ingest")
        report = await ingest_directory(blueprints, blueprints_dir, replace=body.replace)
        return report.to_dict()

    @app.post("/blueprints/search")
    async def search_blueprints(body: SearchIn):
        chunks = await blueprints.search(
            body.query, project_id=body.project_id or project_id,
            zone=body.zone, category=body.category, top_k=body.top_k,
        )
        return {"chunks": [c.to_dict() for c in chunks]}

    # ---------------------------------------------------------------- live push

    @app.websocket("/ws")
    async def websocket_endpoint(
        ws: WebSocket,
        kind: str = "dashboard",
        zone: str | None = None,
        worker_id: str | None = None,
    ):
        """Live push. Devices pass `kind=device&zone=…` to get zone-scoped advisories."""

        await ws.accept()
        client = await hub.connect(ws, kind=kind, zone=zone, worker_id=worker_id)
        try:
            await ws.send_json({"topic": "hello", "zone": zone, "ts": time.time(),
                                "data": {"client_id": client.client_id, "kind": client.kind}})
            while True:
                # the only inbound traffic is keepalive; reading also detects disconnects
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send_json({"topic": "pong", "ts": time.time(), "data": {}})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 — a malformed frame must not spam the log as a crash
            log.debug("ws client %s closed unexpectedly", client.client_id)
        finally:
            await hub.disconnect(client)

    @app.get("/broadcast/clients")
    async def broadcast_clients():
        return {"clients": hub.clients(), "stats": hub.stats()}

    # ---------------------------------------------------------------- workers

    @app.get("/workers")
    async def list_workers():
        alerts = await store.list_alerts(limit=1000)
        workers: dict[str, dict[str, Any]] = {}
        for a in alerts:
            wid = a.get("worker_id")
            if not wid:
                continue
            w = workers.setdefault(wid, {"worker_id": wid, "active_alerts": 0, "zone": a.get("zone")})
            if a["state"] in ("NEW", "ACTIVE"):
                w["active_alerts"] += 1
        return {"workers": sorted(workers.values(), key=lambda w: -w["active_alerts"])}

    @app.get("/workers/{worker_id}/timeline")
    async def worker_timeline(worker_id: str, event_limit: int = Query(default=50, le=500)):
        all_alerts = await store.list_alerts(worker_id=worker_id, limit=500)
        active = [a for a in all_alerts if a["state"] in ("NEW", "ACTIVE")]
        past = [a for a in all_alerts if a["state"] in ("RESOLVED", "SUPPRESSED")]
        recent_events = await events_repo.list_events(worker_id=worker_id, limit=event_limit)

        score = 100
        for a in active:
            score -= _sev_penalty(a["severity"])
        day_ago = time.time() - 86400
        for a in past:
            if (a.get("resolved_at") or a["last_seen"]) >= day_ago:
                score -= _sev_penalty(a["severity"]) // 2
        score = max(0, min(100, score))

        return {
            "worker_id": worker_id,
            "current_zone": active[0]["zone"] if active else (
                recent_events[0]["zone"] if recent_events else None),
            "live_status": "at_risk" if any(a["severity"] in ("high", "critical") for a in active)
                           else ("flagged" if active else "ok"),
            "safety_score": score,
            "active_alerts": active,
            "past_alerts": past[:50],
            "recent_events": recent_events,
        }

    # ---------------------------------------------------------------- control

    @app.post("/control/inspection")
    async def set_inspection(body: InspectionControlIn):
        """Toggle inspection mode on the edge, via the bus (control.inspection)."""

        state = {"enabled": body.enabled}
        await cache.set("control:inspection", state, ttl_s=30 * 24 * 3600)
        await bus.publish("control.inspection", state)
        log.info("inspection mode -> %s (published to bus)", body.enabled)
        return {"enabled": body.enabled}

    @app.get("/control/inspection")
    async def get_inspection():
        state = await cache.get("control:inspection")
        return {"enabled": bool(state and state.get("enabled"))}

    # ---------------------------------------------------------------- misc

    @app.get("/health")
    async def health():
        tracked = await triggers.list_tracked()
        fb = await feedback.stats()
        pending_rfis = await store.list_rfis(status="pending_review", limit=1000)
        embed_probe = await embedder.probe()
        return {
            "status": "ok",
            "tracked_alerts": len(tracked),
            "rules": len(rules_engine.list_rules()),
            "zones": len(await zones.list()),
            "feedback": fb,
            "rfis_pending": len(pending_rfis),
            "learning": await learning.latest_delta(),
            "learning_busy": learning.is_busy(),
            "broadcast": hub.stats(),
            "rag": {
                "available": blueprints.available,
                "indexed_chunks": await blueprints.count(),
                "embeddings": embed_probe,
            },
            "llm_gate": {
                "enabled": verifier is not None,
                "model": verifier.model if verifier else None,
            },
            # The detector itself runs in the edge process, so the backend reports what it can
            # verify from here: whether the configured weights actually exist on disk. A missing
            # file is the difference between "PPE alerts are quiet" and "PPE is silently off".
            "ppe": _ppe_weights_status(cfg),
        }

    return app


def run_backend(config_path: str = "config.yaml", host: str = "0.0.0.0", port: int = 8100) -> int:
    import uvicorn

    cfg = load_config(config_path)
    setup_logging(cfg.get("logging.level", "INFO"))
    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_backend())
