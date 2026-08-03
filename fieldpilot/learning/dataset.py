"""Turn supervisor feedback rows into a YOLO-format training dataset.

An **approved** alert says "the model was right here" → the frame plus its box becomes a positive
example. A **rejected** alert says "there was nothing there" → the frame becomes a negative
(background) example, written as an empty label file, which is how YOLO learns to stop firing on
it. Rows without a readable frame or a usable box are skipped and counted, never silently dropped.

The generated `data.yaml` trains on the feedback images but validates against the **locked** val
set, so the mAP50 delta the gate reads is always measured on data no feedback can contaminate.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.learning.dataset")


@dataclass
class DatasetSummary:
    out_dir: str
    data_yaml: str
    positives: int = 0
    negatives: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    class_names: dict[int, str] = field(default_factory=dict)

    @property
    def usable(self) -> int:
        return self.positives + self.negatives

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["usable"] = self.usable
        return d

    def _skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


def _class_index(label: str | None, class_names: dict[int, str]) -> int | None:
    """Match a feedback label to the base model's label space, case/separator insensitive."""

    if not label:
        return None
    def norm(s: str) -> str:
        return s.lower().replace(" ", "").replace("-", "").replace("_", "")

    target = norm(label)
    for idx, name in class_names.items():
        if norm(name) == target:
            return int(idx)
    # fall back to a containment match ("helmet" ↔ "Hardhat" won't match; "no-hardhat" ↔ "NO-Hardhat" will)
    for idx, name in class_names.items():
        if target and (target in norm(name) or norm(name) in target):
            return int(idx)
    return None


def build_dataset(
    rows: list[dict[str, Any]],
    out_dir: str | Path,
    *,
    class_names: dict[int, str],
    val_images_dir: str | Path | None = None,
) -> DatasetSummary:
    """Materialise `rows` into `out_dir` as a YOLO detection dataset."""

    import cv2

    out = Path(out_dir)
    img_dir = out / "images" / "train"
    lbl_dir = out / "labels" / "train"
    for d in (img_dir, lbl_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    summary = DatasetSummary(
        out_dir=str(out), data_yaml=str(out / "data.yaml"), class_names=dict(class_names)
    )

    for i, row in enumerate(rows):
        image_path = row.get("image_path")
        if not image_path or not Path(image_path).is_file():
            summary._skip("missing_image")
            continue
        frame = cv2.imread(str(image_path))
        if frame is None:
            summary._skip("unreadable_image")
            continue
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            summary._skip("bad_dimensions")
            continue

        stem = f"{i:06d}_{Path(image_path).stem}"[:120]
        decision = str(row.get("decision", "")).lower()
        lines: list[str] = []

        if decision == "approve":
            if not row.get("label"):
                # no class name was recorded at review time, so there is nothing to train toward
                summary._skip("missing_label")
                continue
            cls = _class_index(row.get("label"), class_names)
            if cls is None:
                summary._skip("label_outside_model_classes")
                continue
            bbox = row.get("bbox")
            if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
                summary._skip("missing_bbox")
                continue
            x1, y1, x2, y2 = (float(v) for v in bbox)
            x1, x2 = sorted((max(0.0, x1), min(float(w), x2)))
            y1, y2 = sorted((max(0.0, y1), min(float(h), y2)))
            bw, bh = (x2 - x1) / w, (y2 - y1) / h
            if bw <= 0.001 or bh <= 0.001:
                summary._skip("degenerate_bbox")
                continue
            cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
            lines.append(f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
            summary.positives += 1
        elif decision == "reject":
            # empty label file = explicit background sample
            summary.negatives += 1
        else:
            summary._skip("unknown_decision")
            continue

        shutil.copyfile(image_path, img_dir / f"{stem}.jpg")
        (lbl_dir / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))

    names_block = "\n".join(f"  {i}: {n}" for i, n in sorted(class_names.items()))
    val_ref = str(Path(val_images_dir).resolve()) if val_images_dir else "images/train"
    (out / "data.yaml").write_text(
        "# generated by fieldpilot.learning — train on feedback, validate on the LOCKED set\n"
        f"path: {out.resolve()}\n"
        "train: images/train\n"
        f"val: {val_ref}\n"
        "names:\n"
        f"{names_block}\n"
    )
    log.info(
        "dataset built: %d positive, %d negative, %d skipped → %s",
        summary.positives, summary.negatives, summary.skipped, out,
    )
    return summary
