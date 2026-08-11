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

import hmac
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fieldpilot.auth import (
    SESSIONS_TABLE,
    USERS_TABLE,
    AuthError,
    AuthService,
    Forbidden,
    InvalidCredentials,
    NotAuthenticated,
    bearer_token,
)
from fieldpilot.auth.service import require_role, require_site_manager, require_user
from fieldpilot.backend.service import Orchestrator
from fieldpilot.backend.settings import SETTINGS_TABLE, TRACKED_ITEMS, SettingsService
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
from fieldpilot.workforce import (
    MESSAGES_TABLE,
    OCCUPANCY_TABLE,
    QUESTIONS_TABLE,
    MessageError,
    MessageService,
    OccupancyMismatchError,
    OccupancyService,
    QuestionError,
    QuestionService,
)
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


class TrackedItemIn(BaseModel):
    item_name: str
    enabled: bool


class MonitoringIn(BaseModel):
    confidence_threshold: float | None = None
    pose_enabled: bool | None = None


class ModelSelectIn(BaseModel):
    model_key: str
    download: bool = True


class LoginIn(BaseModel):
    username: str
    password: str


class CreateUserIn(BaseModel):
    username: str
    password: str
    role: str = "worker"
    display_name: str = ""
    worker_id: str | None = None


class QuestionReplyIn(BaseModel):
    reply: str


def _question_out(question: dict[str, Any]) -> dict[str, Any]:
    """Wire shape for a question: expose the photo as a URL, never a filesystem path."""

    out = dict(question)
    path = out.pop("image_path", None)
    out["image_url"] = f"/uploads/{Path(path).name}" if path else None
    out.setdefault("citations", [])
    return out


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
    settings = SettingsService(docs, bus)
    models_dir = str(cfg.get("detection.models_dir", "models"))

    # --- people: who is signed in, where they are, and what they are asking -----------
    auth = AuthService(docs, token_ttl_s=float(cfg.get("auth.token_ttl_s", 43200)))
    occupancy = OccupancyService(docs)
    messages = MessageService(docs)
    uploads_dir = str(cfg.get("storage.uploads_dir", "data/uploads"))
    max_upload = int(cfg.get("storage.max_upload_bytes", 8 * 1024 * 1024))
    questions = QuestionService(
        docs,
        index=blueprints,
        ollama_host=ollama_host,
        model=str(cfg.get("reasoning.llm_model", "llama3.2:3b")),
        project_id=project_id,
        uploads_dir=uploads_dir,
        llm_enabled=bool(cfg.get("llm.enabled", False)) or bool(cfg.get("questions.llm", True)),
    )

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
        await docs.start([
            ZONES_TABLE, FEEDBACK_TABLE, LEARNING_RUNS_TABLE, SETTINGS_TABLE,
            USERS_TABLE, SESSIONS_TABLE, OCCUPANCY_TABLE, QUESTIONS_TABLE, MESSAGES_TABLE,
        ])
        await zones.start()
        await auth.start(seed=None if cfg.get("auth.seed_demo_users", True) else [])
        await occupancy.start()
        await questions.start()
        await settings.start({
            "tracked_items": {
                item: bool((cfg.get("safety.tracked_items") or {}).get(item, item in ("helmet", "vest")))
                for item in TRACKED_ITEMS
            },
            "confidence_threshold": float(cfg.get("detection.conf_min", 0.35)),
            "pose_enabled": True,
            "selected_model": str(cfg.get("detection.ppe_model") or ""),
        })

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
        if not ingest_token:
            # POST /events is how edge devices and models publish — they hold no user session,
            # so it cannot sit behind require_user like the rest of the REST surface. Without a
            # shared secret it is wide open: anyone who can reach this port can inject fabricated
            # hazard events (or silence real ones by flooding a dedup key). Set auth.ingest_token
            # to close this; left unset only because the edge does not yet send one (see app.py's
            # `require_ingest_token`).
            log.warning(
                "auth.ingest_token is not set — POST /events accepts events from anyone who can "
                "reach this port, with no credentials. Set auth.ingest_token to require edge "
                "devices to present a shared secret."
            )
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

    # Auth failures are raised by the framework-agnostic auth service; translate them here so
    # that module never has to import FastAPI.
    @app.exception_handler(NotAuthenticated)
    async def _unauthenticated(_request, exc: NotAuthenticated):
        return JSONResponse(
            {"detail": str(exc) or "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(InvalidCredentials)
    async def _bad_credentials(_request, exc: InvalidCredentials):
        return JSONResponse({"detail": "invalid username or password"}, status_code=401)

    @app.exception_handler(Forbidden)
    async def _forbidden(_request, exc: Forbidden):
        return JSONResponse({"detail": str(exc) or "not permitted for your role"}, status_code=403)

    @app.exception_handler(AuthError)
    async def _auth_error(_request, exc: AuthError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # serve captured alert snapshots (the annotated bbox JPEGs the edge writes)
    import os

    os.makedirs("data/alerts", exist_ok=True)
    app.mount("/images", StaticFiles(directory="data/alerts"), name="alert-images")
    # worker-submitted photos (question attachments, manual hazard reports)
    os.makedirs(uploads_dir, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="worker-uploads")

    any_user = require_user(auth)
    manager_only = require_site_manager(auth)
    worker_only = require_role(auth, "worker")

    # POST /events is the one route a user session cannot gate: it is the ingest path edge
    # devices and models push through (fieldpilot/offline/forwarder.py, fieldpilot/events/bridge
    # publish onto the in-process bus and never touch this HTTP route directly, but the store-
    # and-forward client does). Those clients hold no login, so `require_user` does not apply.
    # `auth.ingest_token`, when set, is a shared secret they present as a bearer token instead;
    # left unset, ingest stays exactly as open as it is today rather than breaking silently.
    ingest_token = cfg.get("auth.ingest_token") or None

    async def require_ingest_token(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> None:
        if not ingest_token:
            return
        presented = bearer_token(authorization)
        # hmac.compare_digest: this is a shared secret, not a per-user session lookup, so the
        # constant-time comparison has to happen here rather than inside AuthService.
        if not presented or not hmac.compare_digest(presented, str(ingest_token)):
            raise HTTPException(401, "missing or invalid ingest token")

    _IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp")
    #: Container formats a phone or browser realistically records a voice note in.
    _AUDIO_TYPES = (".m4a", ".aac", ".mp4", ".mp3", ".ogg", ".opus", ".wav", ".webm", ".3gp")

    async def _save_upload(
        upload: UploadFile | None, *, allowed: tuple[str, ...] = _IMAGE_TYPES,
    ) -> str | None:
        """Persist a worker upload, bounded in size, with a generated name.

        The client's filename is never trusted for the stored name — only its extension, from an
        allowlist — so an upload cannot choose where it lands or what it shadows.
        """

        if upload is None or not upload.filename:
            return None
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in allowed:
            raise HTTPException(400, f"unsupported upload type {suffix!r}")
        data = await upload.read(max_upload + 1)
        if len(data) > max_upload:
            raise HTTPException(413, f"upload exceeds the {max_upload // 1024 // 1024} MiB limit")
        if not data:
            return None
        name = f"{uuid.uuid4().hex}{suffix}"
        Path(uploads_dir).mkdir(parents=True, exist_ok=True)
        (Path(uploads_dir) / name).write_bytes(data)
        return name

    # ---------------------------------------------------------------- auth

    @app.post("/auth/login")
    async def login(body: LoginIn):
        user, token = await auth.authenticate(body.username, body.password)
        log.info("login: %s (%s)", user["username"], user["role"])
        return {"token": token, "user": user}

    @app.post("/auth/logout")
    async def logout(authorization: str | None = Header(default=None, alias="Authorization")):
        token = bearer_token(authorization)
        return {"ok": bool(token) and await auth.logout(token)}

    @app.get("/auth/me")
    async def whoami(user: dict[str, Any] = Depends(any_user)):
        return user

    @app.get("/auth/users")
    async def list_users(_: dict[str, Any] = Depends(manager_only)):
        return {"users": await auth.list_users()}

    @app.post("/auth/users", status_code=201)
    async def create_user(body: CreateUserIn, _: dict[str, Any] = Depends(manager_only)):
        try:
            return await auth.create_user(
                username=body.username, password=body.password, role=body.role,
                display_name=body.display_name, worker_id=body.worker_id,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---------------------------------------------------------------- worker: me

    def _worker_id(user: dict[str, Any]) -> str:
        wid = user.get("worker_id")
        if not wid:
            raise HTTPException(
                409,
                "this account is not linked to a worker id, so it has no site presence. "
                "Ask a site manager to set one.",
            )
        return str(wid)

    @app.get("/me/alerts")
    async def my_alerts(
        limit: int = Query(default=100, le=500),
        user: dict[str, Any] = Depends(worker_only),
    ):
        """Only this worker's alerts. Scoped server-side, not filtered in the client."""

        return {"alerts": await store.list_alerts(worker_id=_worker_id(user), limit=limit)}

    @app.post("/me/alerts", status_code=202)
    async def raise_alert(
        event_type: str = Form(...),
        severity: str = Form("high"),
        message: str = Form(""),
        zone: str | None = Form(None),
        image: UploadFile | None = File(None),
        user: dict[str, Any] = Depends(worker_only),
    ):
        """A worker reports a hazard by hand.

        It enters the platform as a normal canonical event, so it goes through triggers, rules and
        notifications exactly like a machine detection — a human report is not a lesser signal.
        """

        worker_id = _worker_id(user)
        stored_name = await _save_upload(image)
        current = await occupancy.current_zone(worker_id)
        event = Event(
            event_type=event_type if event_type in
            ("ppe", "fall", "proximity", "crack", "inspection", "gas", "fire") else "inspection",
            camera_id=f"worker-{worker_id}",
            worker_id=worker_id,
            zone=zone or (current or {}).get("zone_id"),
            severity=severity if severity in ("low", "medium", "high", "critical") else "high",
            confidence=1.0,          # a person saw it; this is not a model score
            payload={
                "message": (message or "Hazard reported by worker").strip()[:500],
                "reported_by": worker_id,
                "reporter_name": user.get("display_name") or user.get("username"),
                "source": "worker_report",
                "dedup_key": f"worker-report:{uuid.uuid4().hex[:8]}",
                "image_url": f"/uploads/{stored_name}" if stored_name else None,
            },
            image_url=f"/uploads/{stored_name}" if stored_name else None,
        )
        await publish_event(bus, event)
        log.info("worker %s reported a %s hazard in %s", worker_id, event.event_type, event.zone)
        return {"ok": True, "event_id": event.event_id, "zone": event.zone,
                "image_url": event.image_url}

    @app.get("/me/zone")
    async def my_zone(user: dict[str, Any] = Depends(worker_only)):
        current = await occupancy.current_zone(_worker_id(user))
        if current is None:
            return {"occupancy": None}
        zone = await zones.get(str(current.get("zone_id")))
        return {"occupancy": {**current, "zone_name": (zone or {}).get("name")}}

    # ---------------------------------------------------------------- zone presence

    @app.get("/zones/occupancy")
    async def zone_occupancy(_: dict[str, Any] = Depends(any_user)):
        """Who is in which zone, and which zones carry the most warnings."""

        report = await occupancy.occupancy_report(
            await zones.list(), await store.list_alerts(limit=1000)
        )
        names = {u.get("worker_id"): u.get("display_name") for u in await auth.list_users()}
        for row in report:
            for w in row.get("workers", []):
                w["display_name"] = names.get(w.get("worker_id"))
        return {"zones": report}

    @app.post("/zones/{zone_id}/enter")
    async def enter_zone(zone_id: str, user: dict[str, Any] = Depends(worker_only)):
        if await zones.get(zone_id) is None:
            raise HTTPException(404, "zone not found")
        worker_id = _worker_id(user)
        result = await occupancy.enter(worker_id, zone_id)
        await hub.publish("presence", {"worker_id": worker_id, "zone_id": zone_id,
                                       "event": "enter", **result}, zone=zone_id)
        # flatten to the shape the clients are written against: entered_at at the top level, and
        # `closed_previous` naming what actually happened when a worker moves between zones
        occ = result.get("occupancy") or {}
        closed = result.get("closed")
        return {
            "ok": True,
            "zone_id": zone_id,
            "entered_at": occ.get("entered_at"),
            "already_here": not result.get("created", True),
            "closed_previous": (
                {"zone_id": closed.get("zone_id"), "duration_s": closed.get("duration_s")}
                if closed else None
            ),
        }

    @app.post("/zones/{zone_id}/leave")
    async def leave_zone(zone_id: str, user: dict[str, Any] = Depends(worker_only)):
        worker_id = _worker_id(user)
        try:
            result = await occupancy.leave(worker_id, zone_id)
        except OccupancyMismatchError as exc:
            raise HTTPException(409, str(exc)) from exc
        if result is None:
            raise HTTPException(409, "you are not checked in to a zone")
        await hub.publish("presence", {"worker_id": worker_id, "zone_id": zone_id,
                                       "event": "leave", **result}, zone=zone_id)
        return {
            "ok": True,
            "zone_id": result.get("zone_id"),
            "left_at": result.get("left_at"),
            "duration_s": result.get("duration_s"),
        }

    # ---------------------------------------------------------------- worker questions

    @app.post("/questions", status_code=201)
    async def ask_question(
        background: BackgroundTasks,
        text: str = Form(...),
        zone: str | None = Form(None),
        image: UploadFile | None = File(None),
        user: dict[str, Any] = Depends(worker_only),
    ):
        """Ask about something on site. Answered by the LLM *and* routed to the site manager."""

        worker_id = _worker_id(user)
        stored_name = await _save_upload(image)
        current = await occupancy.current_zone(worker_id)
        try:
            question = await questions.ask(
                worker_id=worker_id,
                text=text,
                zone=zone or (current or {}).get("zone_id"),
                image_path=stored_name,
            )
        except QuestionError as exc:
            raise HTTPException(400, str(exc)) from exc

        # the LLM runs after the response: the worker is not kept waiting on a model
        background.add_task(questions.answer_with_llm, question["question_id"])
        # and the manager is told regardless of what the model says
        await notifications.notify(
            dedup_key=f"question:{question['question_id']}",
            subject=f"Question from {user.get('display_name') or worker_id}",
            body=question["text"][:280],
            severity="low",
            channels=["dashboard"],
            meta={"question": question},
        )
        await hub.publish("question", question, audience="dashboard")
        return _question_out(question)

    @app.get("/questions/stats")
    async def question_stats(_: dict[str, Any] = Depends(any_user)):
        return await questions.stats()

    @app.get("/questions")
    async def list_questions(
        status: str | None = None,
        zone: str | None = None,
        limit: int = Query(default=100, le=500),
        user: dict[str, Any] = Depends(any_user),
    ):
        """A worker sees only their own questions; a site manager sees every question."""

        scope = user.get("worker_id") if user.get("role") == "worker" else None
        rows = await questions.list(worker_id=scope, status=status, zone=zone, limit=limit)
        return {"questions": [_question_out(q) for q in rows]}

    @app.get("/questions/{question_id}")
    async def get_question(question_id: str, user: dict[str, Any] = Depends(any_user)):
        question = await questions.get(question_id)
        if question is None:
            raise HTTPException(404, "question not found")
        if user.get("role") == "worker" and question.get("worker_id") != user.get("worker_id"):
            raise HTTPException(403, "not your question")
        return _question_out(question)

    @app.post("/questions/{question_id}/reply")
    async def reply_to_question(
        question_id: str, body: QuestionReplyIn,
        user: dict[str, Any] = Depends(manager_only),
    ):
        """The site manager's answer — the authoritative one."""

        try:
            updated = await questions.reply(
                question_id, manager_id=str(user.get("user_id")), reply=body.reply,
            )
        except QuestionError as exc:
            raise HTTPException(400, str(exc)) from exc
        if updated is None:
            raise HTTPException(404, "question not found")
        await hub.publish("question", updated)
        return _question_out(updated)

    # ---------------------------------------------------------------- direct messages

    def _message_out(record: dict[str, Any]) -> dict[str, Any]:
        """Wire shape: the voice note is a URL, never a filesystem path."""

        out = dict(record)
        path = out.pop("audio_path", None)
        out["audio_url"] = f"/uploads/{Path(path).name}" if path else None
        return out

    async def _send_message(
        *, worker_id: str, sender_role: str, sender_id: str, sender_name: str | None,
        text: str, audio: UploadFile | None,
    ) -> dict[str, Any]:
        """Shared by both directions — identical validation, storage and live push."""

        stored = await _save_upload(audio, allowed=_AUDIO_TYPES)
        try:
            record = await messages.send(
                worker_id=worker_id, sender_role=sender_role, sender_id=sender_id,
                sender_name=sender_name, text=text, audio_path=stored,
            )
        except MessageError as exc:
            raise HTTPException(400, str(exc)) from exc

        out = _message_out(record)
        # Dashboards see every thread; the worker's own device gets only their own message,
        # addressed by worker_id so a colleague's phone never receives someone else's conversation.
        await hub.publish("message", out, audience="dashboard")
        await hub.publish("message", out, audience="device", worker_id=worker_id)
        return out

    @app.get("/messages/threads")
    async def list_threads(_: dict[str, Any] = Depends(manager_only)):
        """The manager's inbox: one entry per worker, most recently active first."""

        return {"threads": await messages.threads()}

    @app.get("/messages/unread")
    async def unread_count(user: dict[str, Any] = Depends(any_user)):
        """Badge count. A manager counts every worker's unread; a worker counts the manager's."""

        if user.get("role") == "site_manager":
            return {"unread": await messages.unread_for_manager()}
        worker_id = _worker_id(user)
        thread = await messages.thread(worker_id)
        return {"unread": sum(
            1 for m in thread if m.get("sender_role") == "site_manager" and not m.get("read_at")
        )}

    @app.get("/messages/{worker_id}")
    async def get_thread(
        worker_id: str,
        limit: int = Query(default=200, le=500),
        user: dict[str, Any] = Depends(any_user),
    ):
        """One conversation. A worker may only ever read their own."""

        if user.get("role") == "worker" and _worker_id(user) != worker_id:
            raise HTTPException(403, "you can only read your own messages")
        rows = await messages.thread(worker_id, limit=limit)
        return {"worker_id": worker_id, "messages": [_message_out(m) for m in rows]}

    @app.post("/messages/{worker_id}/read")
    async def mark_thread_read(worker_id: str, user: dict[str, Any] = Depends(any_user)):
        if user.get("role") == "worker" and _worker_id(user) != worker_id:
            raise HTTPException(403, "you can only read your own messages")
        changed = await messages.mark_read(worker_id, reader_role=str(user.get("role")))
        return {"worker_id": worker_id, "marked_read": changed}

    @app.post("/messages/{worker_id}", status_code=201)
    async def manager_send_message(
        worker_id: str,
        text: str = Form(""),
        audio: UploadFile | None = File(None),
        user: dict[str, Any] = Depends(manager_only),
    ):
        """The site manager writes (or speaks) to one worker."""

        return await _send_message(
            worker_id=worker_id, sender_role="site_manager",
            sender_id=str(user.get("user_id")),
            sender_name=user.get("display_name") or user.get("username"),
            text=text, audio=audio,
        )

    @app.post("/me/messages", status_code=201)
    async def worker_send_message(
        text: str = Form(""),
        audio: UploadFile | None = File(None),
        user: dict[str, Any] = Depends(worker_only),
    ):
        """The worker replies, by text or by holding the microphone."""

        worker_id = _worker_id(user)
        return await _send_message(
            worker_id=worker_id, sender_role="worker", sender_id=str(user.get("user_id")),
            sender_name=user.get("display_name") or user.get("username"),
            text=text, audio=audio,
        )

    @app.get("/me/messages")
    async def worker_thread(
        limit: int = Query(default=200, le=500),
        user: dict[str, Any] = Depends(worker_only),
    ):
        worker_id = _worker_id(user)
        rows = await messages.thread(worker_id, limit=limit)
        return {"worker_id": worker_id, "messages": [_message_out(m) for m in rows]}

    # ---------------------------------------------------------------- events

    @app.post("/events", status_code=202)
    async def ingest_event(
        event: Event, _: None = Depends(require_ingest_token)
    ) -> dict[str, str]:
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
        user: dict[str, Any] = Depends(any_user),
    ):
        # I4: a worker filtering by worker_id must be filtering for themself — otherwise this is
        # exactly how a colleague's raw event history (and whatever a hazard event's payload
        # carries) would leak. `/me/*` exists for a worker's own view; this is the shared board.
        if user.get("role") == "worker" and _worker_id(user) != worker_id:
            raise HTTPException(403, "you can only view your own events")
        return {"events": await events_repo.list_events(
            event_type=event_type, worker_id=worker_id, zone=zone, since=since, limit=limit)}

    @app.get("/events/stats")
    async def event_stats(since: float | None = None, _: dict[str, Any] = Depends(any_user)):
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
        user: dict[str, Any] = Depends(any_user),
    ):
        # I4: same reasoning as /events above — a worker may only ask for their own worker_id,
        # never a colleague's alert history. Their own view without the filter lives at
        # /me/alerts.
        if user.get("role") == "worker" and _worker_id(user) != worker_id:
            raise HTTPException(403, "you can only view your own alerts")
        return {"alerts": await store.list_alerts(
            state=state, severity=severity, worker_id=worker_id, zone=zone,
            event_type=event_type, since=since, limit=limit)}

    @app.get("/alerts/stats")
    async def alert_stats(_: dict[str, Any] = Depends(any_user)):
        """Board summary: totals, today, outstanding, and a per-item breakdown.

        `hrm` surfaced exactly this on its dashboard and it is the shape a site manager wants.
        "Outstanding" means NEW or ACTIVE here — this platform tracks an alert lifecycle rather
        than a single acknowledged flag, so an unresolved alert is the equivalent notion.
        """

        alerts = await store.list_alerts(limit=1000)
        day_start = time.time() - 86400
        by_item: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for a in alerts:
            payload = a.get("payload") or {}
            item = (payload.get("ppe_item") or payload.get("class")
                    or payload.get("defect") or a["event_type"])
            by_item[str(item)] = by_item.get(str(item), 0) + 1
            by_severity[a["severity"]] = by_severity.get(a["severity"], 0) + 1
        return {
            "total": len(alerts),
            "today": sum(1 for a in alerts if a["first_seen"] >= day_start),
            "outstanding": sum(1 for a in alerts if a["state"] in ("NEW", "ACTIVE")),
            "resolved": sum(1 for a in alerts if a["state"] == "RESOLVED"),
            "suppressed": sum(1 for a in alerts if a["state"] == "SUPPRESSED"),
            "disputed": sum(1 for a in alerts
                            if ((a.get("payload") or {}).get("llm_verdict") or {}).get("disputed")),
            "by_item": dict(sorted(by_item.items(), key=lambda kv: -kv[1])),
            "by_severity": by_severity,
        }

    @app.get("/alerts/{alert_id}")
    async def get_alert(alert_id: str, _: dict[str, Any] = Depends(any_user)):
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
    async def resolve_alert(alert_id: str, _: dict[str, Any] = Depends(manager_only)):
        return await _mutate(alert_id, "resolve")

    @app.post("/alerts/{alert_id}/suppress")
    async def suppress_alert(alert_id: str, _: dict[str, Any] = Depends(manager_only)):
        return await _mutate(alert_id, "suppress")

    @app.post("/alerts/{alert_id}/unsuppress")
    async def unsuppress_alert(alert_id: str, _: dict[str, Any] = Depends(manager_only)):
        return await _mutate(alert_id, "unsuppress")

    @app.post("/alerts/{alert_id}/acknowledge")
    async def acknowledge_alert(alert_id: str, _: dict[str, Any] = Depends(manager_only)):
        """Operator has seen and dealt with it.

        `hrm` modelled this as a boolean column. Here the trigger engine already owns an alert
        lifecycle, so acknowledging resolves the alert rather than adding a parallel flag that
        could disagree with `state`.
        """

        return await _mutate(alert_id, "resolve")

    # ---------------------------------------------------------------- rules

    @app.get("/rules")
    async def list_rules(_: dict[str, Any] = Depends(any_user)):
        return {"rules": [r.to_dict() for r in rules_engine.list_rules()]}

    @app.post("/rules", status_code=201)
    async def create_rule(rule_in: RuleIn, _: dict[str, Any] = Depends(manager_only)):
        rule = Rule.from_dict({**rule_in.model_dump(), "rule_id": uuid.uuid4().hex})
        await store.put_rule(rule.to_dict())
        rules_engine.add_rule(rule)
        return rule.to_dict()

    @app.get("/rules/{rule_id}")
    async def get_rule(rule_id: str, _: dict[str, Any] = Depends(any_user)):
        rule = rules_engine.get_rule(rule_id)
        if rule is None:
            raise HTTPException(404, "rule not found")
        return rule.to_dict()

    @app.put("/rules/{rule_id}")
    async def update_rule(
        rule_id: str, rule_in: RuleIn, _: dict[str, Any] = Depends(manager_only)
    ):
        if rules_engine.get_rule(rule_id) is None:
            raise HTTPException(404, "rule not found")
        rule = Rule.from_dict({**rule_in.model_dump(), "rule_id": rule_id})
        await store.put_rule(rule.to_dict())
        rules_engine.add_rule(rule)
        return rule.to_dict()

    @app.delete("/rules/{rule_id}")
    async def delete_rule(rule_id: str, _: dict[str, Any] = Depends(manager_only)):
        if not rules_engine.remove_rule(rule_id):
            raise HTTPException(404, "rule not found")
        await store.delete_rule(rule_id)
        return {"deleted": rule_id}

    # ---------------------------------------------------------------- notifications / rfis / inspections

    @app.get("/notifications")
    async def list_notifications(
        limit: int = Query(default=200, le=1000), _: dict[str, Any] = Depends(any_user)
    ):
        return {"notifications": await store.list_notifications(limit=limit)}

    @app.get("/rfis")
    async def list_rfis(
        status: str | None = None, limit: int = Query(default=200, le=1000),
        _: dict[str, Any] = Depends(any_user),
    ):
        return {"rfis": await store.list_rfis(status=status, limit=limit)}

    @app.get("/rfis/{rfi_id}")
    async def get_rfi(rfi_id: str, _: dict[str, Any] = Depends(any_user)):
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

    # RFI review and inspection sign-off are supervisor decisions in the same sense alert
    # resolution is: a `ReviewIn.reviewer` defaults to "supervisor" precisely because this is not
    # something the platform lets the person being inspected also close out.
    @app.post("/rfis/{rfi_id}/approve")
    async def approve_rfi(rfi_id: str, body: ReviewIn, _: dict[str, Any] = Depends(manager_only)):
        return await _review_rfi(rfi_id, "approved", body)

    @app.post("/rfis/{rfi_id}/reject")
    async def reject_rfi(rfi_id: str, body: ReviewIn, _: dict[str, Any] = Depends(manager_only)):
        return await _review_rfi(rfi_id, "rejected", body)

    @app.get("/inspections")
    async def list_inspections(
        status: str | None = None, limit: int = Query(default=200, le=1000),
        _: dict[str, Any] = Depends(any_user),
    ):
        return {"inspections": await store.list_inspections(status=status, limit=limit)}

    @app.post("/inspections/{inspection_id}/complete")
    async def complete_inspection(
        inspection_id: str, body: ReviewIn, _: dict[str, Any] = Depends(manager_only)
    ):
        if await store.get_inspection(inspection_id) is None:
            raise HTTPException(404, "inspection not found")
        updated = await store.update_inspection(inspection_id, {
            "status": "completed", "notes": body.notes, "completed_at": time.time(),
        })
        if updated is not None:
            await hub.publish("inspection", updated, zone=updated.get("zone"))
        return updated or {}

    # ---------------------------------------------------------------- site config & models

    @app.get("/config")
    async def site_config(_: dict[str, Any] = Depends(manager_only)):
        """Everything an operator can change at runtime, plus what it currently resolves to.

        Manager-only for the whole family, including this read: it names exactly which PPE
        checks and confidence thresholds are live, which is the same "how is detection tuned"
        information the writes below control — not something worth exposing more broadly.
        """

        return {
            **settings.all(),
            "tracked_items": settings.tracked_items(),
            "available_items": list(TRACKED_ITEMS),
            "models_dir": models_dir,
            "ppe_weights": _ppe_weights_status(cfg),
        }

    @app.post("/config/tracked-items")
    async def set_tracked_item(
        body: TrackedItemIn, _: dict[str, Any] = Depends(manager_only)
    ):
        """Enable/disable one PPE check. Pushed to the edge over the bus."""

        try:
            items = await settings.set_tracked_item(body.item_name, body.enabled)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "tracked_items": items}

    @app.post("/config/monitoring")
    async def set_monitoring(body: MonitoringIn, _: dict[str, Any] = Depends(manager_only)):
        try:
            return {"ok": True, **await settings.set_monitoring(
                confidence_threshold=body.confidence_threshold,
                pose_enabled=body.pose_enabled,
            )}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/models")
    async def list_detector_models(_: dict[str, Any] = Depends(any_user)):
        """The detector registry: what is available, downloaded, and licensed how."""

        try:
            from fieldpilot.models_registry import list_models
        except ImportError:  # pragma: no cover - registry is optional at runtime
            raise HTTPException(503, "model registry unavailable") from None
        return {
            "models": list_models(models_dir),
            "selected": settings.get("selected_model"),
        }

    @app.post("/models/select")
    async def select_detector_model(
        body: ModelSelectIn, _: dict[str, Any] = Depends(manager_only)
    ):
        """Choose a detector, fetching + checksum-verifying its weights if needed.

        The edge owns the actual model swap; this records the choice, makes sure the verified
        weights are on disk, and publishes `control.settings` for the edge to act on.
        """

        try:
            from fieldpilot.models_registry import (
                ModelRegistryError,
                ensure_weights,
                get_option,
            )
        except ImportError:  # pragma: no cover
            raise HTTPException(503, "model registry unavailable") from None

        if get_option(body.model_key) is None:
            raise HTTPException(400, f"unknown model key {body.model_key!r}")
        path = None
        if body.download:
            try:
                # `ensure_weights` is a coroutine that already offloads hashing/IO to a thread
                path = str(await ensure_weights(body.model_key, models_dir))
            except ModelRegistryError as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 — surface the real reason to the operator
                raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        await settings.set_selected_model(body.model_key)
        return {"ok": True, "model_key": body.model_key, "weights": path}

    # ---------------------------------------------------------------- zones

    @app.get("/zones")
    async def list_zones(
        project_id: str | None = None, active_only: bool = False,
        _: dict[str, Any] = Depends(any_user),
    ):
        return {"zones": await zones.list(project_id=project_id, active_only=active_only)}

    @app.post("/zones", status_code=201)
    async def create_zone(body: ZoneIn, _: dict[str, Any] = Depends(manager_only)):
        try:
            zone = await zones.create(body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await hub.publish("zone", zone)
        return zone

    @app.get("/zones/{zone_id}")
    async def get_zone(zone_id: str, _: dict[str, Any] = Depends(any_user)):
        zone = await zones.get(zone_id)
        if zone is None:
            raise HTTPException(404, "zone not found")
        return zone

    @app.put("/zones/{zone_id}")
    async def update_zone(
        zone_id: str, body: ZoneIn, _: dict[str, Any] = Depends(manager_only)
    ):
        try:
            zone = await zones.update(zone_id, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if zone is None:
            raise HTTPException(404, "zone not found")
        await hub.publish("zone", zone)
        return zone

    @app.delete("/zones/{zone_id}")
    async def delete_zone(zone_id: str, _: dict[str, Any] = Depends(manager_only)):
        if not await zones.delete(zone_id):
            raise HTTPException(404, "zone not found")
        return {"deleted": True, "zone_id": zone_id}

    # ---------------------------------------------------------------- feedback

    @app.post("/alerts/{alert_id}/feedback")
    async def submit_feedback(
        alert_id: str, body: FeedbackIn, _: dict[str, Any] = Depends(manager_only)
    ):
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
        _: dict[str, Any] = Depends(any_user),
    ):
        return {"feedback": await feedback.list(
            decision=decision, event_type=event_type, alert_id=alert_id,
            unconsumed_only=unconsumed_only, limit=limit)}

    @app.get("/feedback/stats")
    async def feedback_stats(_: dict[str, Any] = Depends(any_user)):
        return await feedback.stats()

    # ---------------------------------------------------------------- learning

    @app.post("/learning/train")
    async def start_training(body: TrainIn, _: dict[str, Any] = Depends(manager_only)):
        try:
            return await learning.start_run(
                epochs=body.epochs, base_weights=body.base_weights
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/learning/runs")
    async def list_learning_runs(
        limit: int = Query(default=50, le=500), _: dict[str, Any] = Depends(any_user)
    ):
        return {"runs": await learning.list_runs(limit=limit)}

    @app.get("/learning/latest")
    async def latest_learning_run(_: dict[str, Any] = Depends(any_user)):
        return await learning.latest_delta()

    @app.get("/learning/runs/{run_id}")
    async def get_learning_run(run_id: str, _: dict[str, Any] = Depends(any_user)):
        run = await learning.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    # ---------------------------------------------------------------- blueprints / RAG

    @app.get("/blueprints")
    async def blueprint_status(_: dict[str, Any] = Depends(any_user)):
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
    async def ingest_blueprints(body: IngestIn, _: dict[str, Any] = Depends(manager_only)):
        if not blueprints.available and not await blueprints.start():
            raise HTTPException(503, "Qdrant unavailable — cannot ingest")
        report = await ingest_directory(blueprints, blueprints_dir, replace=body.replace)
        return report.to_dict()

    @app.post("/blueprints/search")
    async def search_blueprints(body: SearchIn, _: dict[str, Any] = Depends(any_user)):
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
    async def broadcast_clients(_: dict[str, Any] = Depends(any_user)):
        return {"clients": hub.clients(), "stats": hub.stats()}

    # ---------------------------------------------------------------- workers

    @app.get("/workers")
    async def list_workers(_: dict[str, Any] = Depends(any_user)):
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
    async def worker_timeline(
        worker_id: str, event_limit: int = Query(default=50, le=500),
        user: dict[str, Any] = Depends(any_user),
    ):
        # I4: this is the other route the brief names by name — a worker's 360° view is exactly
        # the "colleague's alert history" an IDOR would leak; only the account's own worker_id
        # (or a site manager, who legitimately sees everyone's) may look.
        if user.get("role") == "worker" and _worker_id(user) != worker_id:
            raise HTTPException(403, "you can only view your own timeline")
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
    async def set_inspection(
        body: InspectionControlIn, _: dict[str, Any] = Depends(manager_only)
    ):
        """Toggle inspection mode on the edge, via the bus (control.inspection)."""

        state = {"enabled": body.enabled}
        await cache.set("control:inspection", state, ttl_s=30 * 24 * 3600)
        await bus.publish("control.inspection", state)
        log.info("inspection mode -> %s (published to bus)", body.enabled)
        return {"enabled": body.enabled}

    @app.get("/control/inspection")
    async def get_inspection(_: dict[str, Any] = Depends(any_user)):
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
