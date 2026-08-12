# FieldPilot AI — project command hub.
# `make help` lists every target with its description.

UV            ?= uv
BACKEND_PORT  ?= 8100
GUI_PORT      ?= 8000
FRONTEND_PORT ?= 3000
CONFIG        ?= config.yaml
DOCKER        ?= docker
COMPOSE       ?= $(DOCKER) compose

.DEFAULT_GOAL := help
.PHONY: help setup setup-backend setup-frontend doctor demo-check \
        fetch-models fetch-ppe-data prepare-ppe-data val-set-demo audit-ppe train-ppe \
        infra-up infra-down infra-ps infra-logs \
        backend edge edge-synthetic gui frontend frontend-build frontend-install \
        run-all stop-all \
        inspect-on inspect-off inspect-status demo-events \
        llm-pull llm-on \
        ingest-blueprints blueprints-status blueprints-search \
        train learning-runs feedback-stats models zones rfis \
        test test-frontend lint lint-frontend validate bench demo-alert measure clean

# ------------------------------------------------------------------ setup

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: setup-backend setup-frontend ## Install ALL deps — backend (uv) + frontend (npm)

setup-backend: ## Install backend Python dependencies (uv)
	$(UV) sync --extra dev --extra server

setup-frontend: ## Install frontend Node dependencies (npm)
	cd frontend && npm install

doctor: ## Check the local environment (uv, docker, camera, espeak, python)
	@scripts/doctor.sh

demo-check: ## Verify hackathon assets: vision weights, Gemma, frontend deps, and environment
	@uv run python scripts/demo_check.py

# ------------------------------------------------------------------ models & datasets

fetch-models: ## Download the model weights into models/ (pose + PPE); ONLY=pose|ppe|damage
	$(UV) run python scripts/fetch_models.py $(if $(ONLY),--only $(ONLY),)

fetch-ppe-data: ## Download and extract the licensed public PPE training sources
	$(UV) run python scripts/fetch_ppe_data.py

prepare-ppe-data: ## Merge/remap sources into the runtime 10-class PPE dataset
	$(UV) run python scripts/prepare_ppe_dataset.py

val-set-demo: ## Generate a SYNTHETIC demo locked val set in data/val_set (unblocks the mAP50 gate)
	$(UV) run python scripts/make_val_set.py

audit-ppe: ## Audit a site YOLO dataset before training: make audit-ppe DATA=data/site/data.yaml
	@test -n "$(DATA)" || (echo "usage: make audit-ppe DATA=/path/to/data.yaml"; exit 2)
	$(UV) run python scripts/train_ppe.py --data "$(DATA)" --audit-only

train-ppe: ## Transfer-learn PPE weights and gate them: make train-ppe DATA=... EPOCHS=60
	@test -n "$(DATA)" || (echo "usage: make train-ppe DATA=/path/to/data.yaml [EPOCHS=60]"; exit 2)
	$(UV) run python scripts/train_ppe.py --data "$(DATA)" \
		$(if $(EPOCHS),--epochs $(EPOCHS),) \
		$(if $(BATCH),--batch $(BATCH),) \
		$(if $(WORKERS),--workers $(WORKERS),)

# ------------------------------------------------------------------ infrastructure

infra-up: ## Start PostgreSQL, Redis, Qdrant, Ollama (docker compose)
	$(COMPOSE) up -d

infra-down: ## Stop the infrastructure stack
	$(COMPOSE) down

infra-ps: ## Show infrastructure container status
	$(COMPOSE) ps

infra-logs: ## Tail infrastructure logs
	$(COMPOSE) logs -f

# ------------------------------------------------------------------ run services

backend: ## Run the event-driven backend (bus + triggers + rules + REST) on :8100
	$(UV) run python -m fieldpilot.run --backend --port $(BACKEND_PORT)

edge: ## Run the edge safety loop on the webcam, publishing events to the bus
	$(UV) run python -m fieldpilot.run --source webcam --bus --config $(CONFIG)

edge-synthetic: ## Run the edge loop on synthetic frames (no camera needed)
	$(UV) run python -m fieldpilot.run --source synthetic --bus --config $(CONFIG)

gui: ## Run the live MJPEG dashboard on :8000 (uses its own camera pipeline)
	$(UV) run python -m fieldpilot.run --gui --port $(GUI_PORT) --config $(CONFIG)

frontend: ## Run the Next.js site-manager dashboard on :3000 (dev mode)
	cd frontend && npm run dev -- --port $(FRONTEND_PORT)

frontend-build: ## Production build of the Next.js dashboard
	cd frontend && npm run build

frontend-install: ## Install frontend dependencies
	cd frontend && npm ci

# ------------------------------------------------------------------ orchestration

run-all: ## Start EVERYTHING: infra + backend + edge feed + Next.js dashboard (Ctrl-C stops all)
	@scripts/run_all.sh

stop-all: ## Stop all services started by run-all + the infra stack
	@scripts/stop_all.sh

# ------------------------------------------------------------------ inspection control

inspect-on: ## Turn ON structural-damage inspection mode (toggles the edge detector via the bus)
	curl -s -X POST http://localhost:$(BACKEND_PORT)/control/inspection \
		-H 'Content-Type: application/json' -d '{"enabled":true}' && echo

inspect-off: ## Turn OFF inspection mode
	curl -s -X POST http://localhost:$(BACKEND_PORT)/control/inspection \
		-H 'Content-Type: application/json' -d '{"enabled":false}' && echo

inspect-status: ## Show current inspection mode state
	curl -s http://localhost:$(BACKEND_PORT)/control/inspection && echo

llm-pull: ## Pull the LLM + embedding models (alert verdicts, RFI drafting, blueprint search)
	@if command -v ollama >/dev/null 2>&1; then \
		ollama pull gemma4:e4b-it-qat && ollama pull nomic-embed-text; \
	else \
		docker compose exec -T ollama ollama pull gemma4:e4b-it-qat && \
		docker compose exec -T ollama ollama pull nomic-embed-text; \
	fi

# ------------------------------------------------------------------ RAG & learning loop

ingest-blueprints: ## Index data/blueprints/ into Qdrant (REPLACE=1 to rebuild from scratch)
	curl -s -X POST http://localhost:$(BACKEND_PORT)/blueprints/ingest \
		-H 'Content-Type: application/json' \
		-d '{"replace": $(if $(REPLACE),true,false)}' && echo

blueprints-status: ## Show the indexed blueprint corpus + embedding backend
	curl -s http://localhost:$(BACKEND_PORT)/blueprints && echo

blueprints-search: ## Search the specs: make blueprints-search Q="rebar spacing" ZONE=zone-a
	curl -s -X POST http://localhost:$(BACKEND_PORT)/blueprints/search \
		-H 'Content-Type: application/json' \
		-d '{"query":"$(Q)"$(if $(ZONE),$(,)"zone":"$(ZONE)")}' && echo

train: ## Fine-tune on supervisor feedback and gate on the mAP50 delta (EPOCHS=n)
	curl -s -X POST http://localhost:$(BACKEND_PORT)/learning/train \
		-H 'Content-Type: application/json' \
		-d '{$(if $(EPOCHS),"epochs":$(EPOCHS))}' && echo

learning-runs: ## Show fine-tune history with the measured mAP50 delta per run
	curl -s http://localhost:$(BACKEND_PORT)/learning/runs && echo

feedback-stats: ## Show supervisor approve/reject counts feeding the learning loop
	curl -s http://localhost:$(BACKEND_PORT)/feedback/stats && echo

models: ## List the detector registry (downloaded / licence / capability)
	@curl -s http://localhost:$(BACKEND_PORT)/models | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"selected: {d['selected']}\"); [print(f\"  {m['key']:<22} {m['capability']:<7} {str(m.get('license')):<14} downloaded={m['downloaded']}\") for m in d['models']]"

zones: ## List the site zone registry
	curl -s http://localhost:$(BACKEND_PORT)/zones && echo

rfis: ## List RFIs awaiting human review
	curl -s "http://localhost:$(BACKEND_PORT)/rfis?status=pending_review" && echo

llm-on: ## Enable the LLM verification gate (FIELDPILOT_LLM__ENABLED=true) — restart backend after
	curl -s -X POST http://localhost:$(BACKEND_PORT)/control/inspection -H 'Content-Type: application/json' -d '{"enabled":true}' > /dev/null
	@echo "note: set FIELDPILOT_LLM__ENABLED=true and FIELDPILOT_LLM__ENABLED in config or env, then make stop-all && make run-all"

# ------------------------------------------------------------------ seed test data

demo-events: ## Post sample events (PPE + proximity + crack + measurement) for a quick demo
	@curl -s -X POST http://localhost:$(BACKEND_PORT)/events -H 'Content-Type: application/json' \
		-d '{"worker_id":"w-1","camera_id":"cam-1","zone":"zone-a","event_type":"proximity","severity":"high","confidence":0.9,"payload":{"dedup_key":"prox-1","message":"near excavator"}}' > /dev/null
	@curl -s -X POST http://localhost:$(BACKEND_PORT)/events -H 'Content-Type: application/json' \
		-d '{"worker_id":"w-1","camera_id":"cam-1","zone":"zone-a","event_type":"ppe","severity":"medium","confidence":0.92,"payload":{"ppe_item":"helmet","dedup_key":"helmet","message":"no helmet"}}' > /dev/null
	@curl -s -X POST http://localhost:$(BACKEND_PORT)/events -H 'Content-Type: application/json' \
		-d '{"camera_id":"cam-2","zone":"zone-b","event_type":"crack","severity":"high","confidence":0.95,"payload":{"defect":"Severerotation","severity_score":0.91,"dedup_key":"sev-1","message":"severe rotation crack"}}' > /dev/null
	@curl -s -X POST http://localhost:$(BACKEND_PORT)/events -H 'Content-Type: application/json' \
		-d '{"camera_id":"cam-3","zone":"zone-c","event_type":"measurement","severity":"medium","confidence":0.9,"payload":{"element":"rebar_spacing","deviation_mm":27.5,"dedup_key":"rebar"}}' > /dev/null
	@echo "Posted 4 sample events → check http://localhost:$(FRONTEND_PORT)/alerts"

# ------------------------------------------------------------------ quality & dev

test: ## Run the full backend test suite
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run pytest -p pytest_asyncio.plugin tests -q

test-frontend: ## Type-check the frontend (tsc)
	cd frontend && npx tsc --noEmit

lint: lint-frontend ## Lint backend (ruff) + frontend (eslint)
	$(UV) run ruff check fieldpilot tests

lint-frontend: ## Lint the frontend with eslint
	cd frontend && npm run lint

validate: ## Headless 10-minute synthetic stress run (stability check)
	$(UV) run python -m fieldpilot.run --validate 10min

bench: ## Latency harness (detection→alert, budget < 500 ms)
	$(UV) run python -m fieldpilot.run --bench

demo-alert: ## Play one sample alert per category (audio/haptics check)
	$(UV) run python -m fieldpilot.run --demo-alert

measure: ## Calibrate px→mm from an image: make measure IMAGE=path/to/img.jpg
	$(UV) run python -m fieldpilot.run --measure $(IMAGE)

clean: ## Remove local data DBs, logs, caches, frontend build (models/weights kept)
	rm -f data/*.db data/events.log.jsonl
	rm -rf data/logs data/tts_cache frontend/.next frontend/out
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
