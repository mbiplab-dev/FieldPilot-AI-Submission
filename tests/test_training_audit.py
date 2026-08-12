from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import yaml

from fieldpilot.learning.audit import audit_yolo_dataset


def _image(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), np.full((32, 32, 3), value, dtype=np.uint8))
    label = Path(str(path).replace("/images/", "/labels/")).with_suffix(".txt")
    label.parent.mkdir(parents=True, exist_ok=True)
    label.write_text("0 0.5 0.5 0.4 0.4\n", encoding="utf-8")


def _dataset(tmp_path: Path) -> Path:
    _image(tmp_path / "images/train/a.jpg", 10)
    _image(tmp_path / "images/val/b.jpg", 20)
    data = tmp_path / "data.yaml"
    data.write_text(
        yaml.safe_dump({
            "path": str(tmp_path),
            "train": "images/train",
            "val": "images/val",
            "names": {0: "Hardhat"},
        }),
        encoding="utf-8",
    )
    return data


def test_valid_dataset_passes_with_small_dataset_warnings(tmp_path):
    report = audit_yolo_dataset(_dataset(tmp_path))

    assert report.ok
    assert report.train_images == report.val_images == 1
    assert report.train_class_counts == {0: 1}
    assert any("high variance" in warning for warning in report.warnings)


def test_train_val_duplicate_is_rejected(tmp_path):
    data = _dataset(tmp_path)
    (tmp_path / "images/val/b.jpg").write_bytes((tmp_path / "images/train/a.jpg").read_bytes())

    report = audit_yolo_dataset(data)

    assert not report.ok
    assert any("leakage" in error for error in report.errors)


def test_malformed_or_missing_labels_are_rejected(tmp_path):
    data = _dataset(tmp_path)
    (tmp_path / "labels/train/a.txt").write_text("0 2 0.5 0.4 0.4\n", encoding="utf-8")
    (tmp_path / "labels/val/b.txt").unlink()

    report = audit_yolo_dataset(data)

    assert not report.ok
    assert any("normalized" in error for error in report.errors)
    assert any("missing label" in error for error in report.errors)
