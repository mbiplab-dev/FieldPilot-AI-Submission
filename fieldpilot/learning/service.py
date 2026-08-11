"""The measure-and-gate learning loop.

    feedback → dataset → baseline mAP50 → fine-tune → candidate mAP50 → delta → promote?

The gate is the point of the module: candidate weights are promoted **only** if mAP50 on the
locked validation set did not regress. A regression is recorded as a completed run with
`promoted: false`, not swept away — `docs/plan.md` explicitly rejects "guaranteed improvement"
framing, so the loop reports what happened.

Training is long and blocking, so a run executes on a worker thread and the API returns a
`run_id` immediately; callers poll `GET /learning/runs/{id}`.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fieldpilot.feedback import FeedbackService
from fieldpilot.learning.dataset import build_dataset
from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.learning")

LEARNING_RUNS_TABLE = TableSpec(
    "learning_runs",
    key="run_id",
    columns=(
        Column("status", indexed=True),   # pending|running|completed|failed|blocked
        Column("base_weights"),
        Column("weights_path"),           # promoted weights, when promoted
        Column("dataset_dir"),
        Column("samples", "int"),
        Column("epochs", "int"),
        Column("map50_before", "real"),
        Column("map50_after", "real"),
        Column("delta", "real"),
        Column("promoted", "bool", indexed=True),
        Column("message"),
        Column("created_at", "real"),
        Column("finished_at", "real"),
    ),
)

_TERMINAL = ("completed", "failed", "blocked")


class LearningService:
    """Owns learning-run records and executes them one at a time."""

    def __init__(
        self,
        store: DocStore,
        feedback: FeedbackService,
        *,
        base_weights: str = "models/ppe_css.pt",
        val_set: str = "data/val_set",
        output_dir: str = "models/finetuned",
        epochs: int = 20,
        promote_if_delta_gte: float = 0.0,
        min_samples: int = 8,
    ) -> None:
        self._table = store.table(LEARNING_RUNS_TABLE)
        self._feedback = feedback
        self.base_weights = base_weights
        self.val_set = val_set
        self.output_dir = output_dir
        self.epochs = epochs
        self.promote_if_delta_gte = promote_if_delta_gte
        self.min_samples = min_samples
        self._running: asyncio.Task[None] | None = None

    # -- run records -----------------------------------------------------------

    async def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._table.list(limit=limit)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._table.get(run_id)

    async def latest_delta(self) -> dict[str, Any] | None:
        """Most recent completed run, for the dashboard's accuracy panel."""

        runs = await self._table.list(where={"status": "completed"}, limit=1)
        return runs[0] if runs else None

    def is_busy(self) -> bool:
        return self._running is not None and not self._running.done()

    # -- orchestration ---------------------------------------------------------

    async def start_run(
        self, *, epochs: int | None = None, base_weights: str | None = None
    ) -> dict[str, Any]:
        """Claim feedback, create a run record, and kick off training in the background."""

        if self.is_busy():
            raise RuntimeError("a learning run is already in progress")

        run_id = uuid.uuid4().hex[:12]
        base = base_weights or self.base_weights
        run = await self._table.put({
            "run_id": run_id,
            "status": "pending",
            "base_weights": base,
            "epochs": int(epochs or self.epochs),
            "samples": 0,
            "promoted": False,
            "message": "claiming feedback",
            "created_at": time.time(),
        })

        rows = await self._feedback.claim_for_training(run_id)
        if len(rows) < self.min_samples:
            await self._feedback.release(run_id)
            return await self._finish(
                run_id, "blocked",
                message=(f"need at least {self.min_samples} unreviewed feedback samples, "
                         f"have {len(rows)}"),
            )

        val_yaml_ok, val_msg = self._check_val_set()
        if not val_yaml_ok:
            await self._feedback.release(run_id)
            return await self._finish(run_id, "blocked", message=val_msg)

        await self._table.patch(run_id, {"samples": len(rows), "message": "training"})
        self._running = asyncio.create_task(self._execute(run_id, rows, base, int(epochs or self.epochs)))
        return (await self.get_run(run_id)) or run

    def _check_val_set(self) -> tuple[bool, str]:
        val_images = Path(self.val_set) / "images"
        if not val_images.is_dir() or not any(val_images.iterdir()):
            return False, (
                f"locked validation set is empty ({val_images}). Populate it with labelled "
                "frames (images/ + labels/) — run `make val-set-demo` to generate a "
                "demonstration set — then retry. The mAP50 gate refuses to run without it."
            )
        return True, ""

    async def _execute(
        self, run_id: str, rows: list[dict[str, Any]], base: str, epochs: int
    ) -> None:
        try:
            await self._table.patch(run_id, {"status": "running"})
            result = await asyncio.to_thread(self._train_and_gate, run_id, rows, base, epochs)
            await self._finish(run_id, "completed", **result)
        except Exception as exc:  # noqa: BLE001 — a failed run must not take the backend down
            log.exception("learning run %s failed", run_id)
            await self._feedback.release(run_id)
            await self._finish(run_id, "failed", message=f"{type(exc).__name__}: {exc}")

    def _train_and_gate(
        self, run_id: str, rows: list[dict[str, Any]], base: str, epochs: int
    ) -> dict[str, Any]:
        """Blocking: build dataset, measure baseline, fine-tune, measure candidate, gate."""

        from ultralytics import YOLO

        base_path = Path(base)
        if not base_path.is_file():
            raise FileNotFoundError(
                f"base weights not found: {base}. Run `make fetch-models` first."
            )

        model = YOLO(str(base_path))
        class_names = {int(k): str(v) for k, v in model.names.items()}

        dataset_dir = Path(self.output_dir) / run_id / "dataset"
        summary = build_dataset(
            rows, dataset_dir,
            class_names=class_names,
            val_images_dir=Path(self.val_set) / "images",
        )
        if summary.usable < self.min_samples:
            raise ValueError(
                f"only {summary.usable} of {len(rows)} feedback rows were usable "
                f"(skipped: {summary.skip_reasons}) — need {self.min_samples}"
            )

        data_yaml = summary.data_yaml

        # 1. baseline on the LOCKED val set
        map50_before = _map50(YOLO(str(base_path)).val(
            data=data_yaml, split="val", verbose=False, plots=False,
        ))
        log.info("run %s baseline mAP50=%.4f", run_id, map50_before)

        # 2. fine-tune
        project = str(Path(self.output_dir) / run_id)
        trained = YOLO(str(base_path))
        trained.train(
            data=data_yaml, epochs=epochs, imgsz=640, project=project, name="train",
            exist_ok=True, verbose=False, plots=False,
        )
        best = Path(project) / "train" / "weights" / "best.pt"
        if not best.is_file():
            raise RuntimeError(f"training produced no weights at {best}")

        # 3. candidate on the SAME locked val set
        map50_after = _map50(YOLO(str(best)).val(
            data=data_yaml, split="val", verbose=False, plots=False,
        ))
        delta = map50_after - map50_before
        log.info("run %s candidate mAP50=%.4f (delta %+.4f)", run_id, map50_after, delta)

        # 4. the gate
        promoted = delta >= self.promote_if_delta_gte
        weights_path = None
        if promoted:
            promoted_to = Path(self.output_dir) / f"promoted_{Path(base).stem}.pt"
            promoted_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(best, promoted_to)
            weights_path = str(promoted_to)
            message = (f"promoted: mAP50 {map50_before:.4f} → {map50_after:.4f} "
                       f"(delta {delta:+.4f} ≥ {self.promote_if_delta_gte})")
        else:
            message = (f"NOT promoted: mAP50 {map50_before:.4f} → {map50_after:.4f} "
                       f"(delta {delta:+.4f} < {self.promote_if_delta_gte}) — regression recorded")
        log.info("run %s %s", run_id, message)

        return {
            "dataset_dir": summary.out_dir,
            "samples": summary.usable,
            "map50_before": round(map50_before, 6),
            "map50_after": round(map50_after, 6),
            "delta": round(delta, 6),
            "promoted": promoted,
            "weights_path": weights_path,
            "message": message,
            "dataset_summary": summary.to_dict(),
        }

    async def _finish(self, run_id: str, status: str, **fields: Any) -> dict[str, Any]:
        assert status in _TERMINAL or status == "running"
        patch = {"status": status, "finished_at": time.time(), **fields}
        updated = await self._table.patch(run_id, patch)
        return updated or {"run_id": run_id, "status": status, **fields}


def _map50(metrics: Any) -> float:
    """Pull mAP50 out of an ultralytics metrics object across versions."""

    box = getattr(metrics, "box", None)
    for holder, attr in ((box, "map50"), (metrics, "map50")):
        val = getattr(holder, attr, None) if holder is not None else None
        if val is not None:
            return float(val)
    results = getattr(metrics, "results_dict", None) or {}
    for key in ("metrics/mAP50(B)", "metrics/mAP50", "mAP50"):
        if key in results:
            return float(results[key])
    raise RuntimeError(f"could not read mAP50 from metrics: {type(metrics).__name__}")
