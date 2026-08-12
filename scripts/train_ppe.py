#!/usr/bin/env python3
"""Transfer-learn the PPE detector and promote only a measured non-regression."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fieldpilot.learning.audit import DatasetAudit, audit_yolo_dataset


def _metrics(result: Any) -> dict[str, float]:
    box = getattr(result, "box", None)
    if box is None:
        raise RuntimeError("Ultralytics returned no box metrics")
    return {
        "precision": float(box.mp),
        "recall": float(box.mr),
        "map50": float(box.map50),
        "map50_95": float(box.map),
    }


def _print_audit(audit: DatasetAudit) -> None:
    print(
        f"dataset: {audit.train_images} train / {audit.val_images} val images; "
        f"{audit.train_instances} train / {audit.val_instances} val boxes"
    )
    for class_id, name in audit.class_names.items():
        print(
            f"  {class_id:>2} {name:<20} "
            f"train={audit.train_class_counts.get(class_id, 0):>5} "
            f"val={audit.val_class_counts.get(class_id, 0):>5}"
        )
    for warning in audit.warnings:
        print(f"WARNING: {warning}")
    for error in audit.errors:
        print(f"ERROR: {error}")


def _same_classes(model_names: dict[int, str], audit: DatasetAudit) -> bool:
    normalized = {int(key): str(value).strip().lower() for key, value in model_names.items()}
    dataset = {key: value.strip().lower() for key, value in audit.class_names.items()}
    return normalized == dataset


def _gate(
    before: dict[str, float], after: dict[str, float], *, recall_tolerance: float
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for metric in ("map50", "map50_95"):
        if after[metric] < before[metric]:
            reasons.append(f"{metric} regressed {before[metric]:.4f} → {after[metric]:.4f}")
    if after["recall"] < before["recall"] - recall_tolerance:
        reasons.append(
            f"recall regressed beyond tolerance: {before['recall']:.4f} → "
            f"{after['recall']:.4f}"
        )
    return not reasons, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="site YOLO data.yaml with separate train/val")
    parser.add_argument("--base", default="models/ppe_css.pt", help="pretrained PPE checkpoint")
    parser.add_argument("--output", default="models/finetuned", help="training run directory")
    parser.add_argument("--promote-to", default="models/finetuned/promoted_ppe.pt")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=float, default=-1, help="-1 lets Ultralytics size the GPU")
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="data-loader processes; one avoids RAM/swap pressure on the 16 GB demo laptop",
    )
    parser.add_argument("--device", default=None, help="0 for CUDA GPU, or cpu")
    parser.add_argument("--recall-tolerance", type=float, default=0.01)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--allow-class-change", action="store_true")
    parser.add_argument(
        "--allow-small-dataset",
        action="store_true",
        help="override minimum sample checks for pipeline debugging; never for a promoted model",
    )
    args = parser.parse_args(argv)

    audit = audit_yolo_dataset(args.data)
    _print_audit(audit)
    if not audit.ok:
        return 1

    from ultralytics import YOLO

    base_path = Path(args.base).expanduser().resolve()
    if not base_path.is_file():
        print(f"ERROR: base checkpoint not found: {base_path}")
        return 1
    base = YOLO(str(base_path))
    model_names = {int(key): str(value) for key, value in base.names.items()}
    if not args.allow_class_change and not _same_classes(model_names, audit):
        print("ERROR: dataset class IDs/names differ from the runtime PPE checkpoint")
        print(f"  model:   {model_names}")
        print(f"  dataset: {audit.class_names}")
        print("Use the same 10 classes, or pass --allow-class-change and update runtime mappings.")
        return 1
    if args.audit_only:
        print("Dataset audit passed; no training started.")
        return 0
    readiness_errors: list[str] = []
    if audit.train_images < 100:
        readiness_errors.append("at least 100 varied training images are required")
    if audit.val_images < 50:
        readiness_errors.append("at least 50 untouched validation images are required")
    for class_id, name in audit.class_names.items():
        if audit.train_class_counts.get(class_id, 0) < 20:
            readiness_errors.append(f"class {class_id} ({name}) needs at least 20 train boxes")
        if audit.val_class_counts.get(class_id, 0) < 10:
            readiness_errors.append(f"class {class_id} ({name}) needs at least 10 val boxes")
    if readiness_errors and not args.allow_small_dataset:
        for error in readiness_errors:
            print(f"ERROR: {error}")
        print("Collect more labels. --allow-small-dataset is only for testing the pipeline.")
        return 1

    run_id = datetime.now(UTC).strftime("ppe_%Y%m%dT%H%M%SZ")
    run_dir = Path(args.output).expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    batch: int | float = int(args.batch) if args.batch.is_integer() else args.batch
    print("Evaluating pretrained baseline on the locked site validation split…")
    before = _metrics(base.val(data=args.data, split="val", plots=False, verbose=False))
    print(f"baseline: {before}")

    print(f"Fine-tuning for up to {args.epochs} epochs → {run_dir}")
    trained = YOLO(str(base_path))
    trained.train(
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=batch,
        patience=args.patience,
        workers=args.workers,
        device=args.device,
        project=str(run_dir),
        name="train",
        exist_ok=True,
        seed=42,
        deterministic=True,
        amp=True,
        plots=True,
    )
    candidate = run_dir / "train" / "weights" / "best.pt"
    if not candidate.is_file():
        raise RuntimeError(f"training produced no candidate checkpoint at {candidate}")
    print("Evaluating candidate on the same untouched validation split…")
    after = _metrics(YOLO(str(candidate)).val(
        data=args.data, split="val", plots=True, verbose=False
    ))
    promoted, reasons = _gate(before, after, recall_tolerance=args.recall_tolerance)
    report = {
        "run_id": run_id,
        "base": str(base_path),
        "candidate": str(candidate),
        "promoted": promoted,
        "rejection_reasons": reasons,
        "metrics_before": before,
        "metrics_after": after,
        "deltas": {key: after[key] - before[key] for key in before},
        "audit": audit.to_dict(),
    }
    (run_dir / "fieldpilot-training-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    if promoted:
        destination = Path(args.promote_to).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)
        print(f"PROMOTED: {destination}")
        print("Set detection.ppe_model to this path, restart, and run a staged site acceptance test.")
        return 0
    print("NOT PROMOTED: " + "; ".join(reasons))
    return 2


if __name__ == "__main__":
    sys.exit(main())
