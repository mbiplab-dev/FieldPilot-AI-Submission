"""Strict checks for a site-specific YOLO safety dataset before fine-tuning."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


@dataclass
class DatasetAudit:
    data_yaml: str
    train_images: int = 0
    val_images: int = 0
    train_instances: int = 0
    val_instances: int = 0
    class_names: dict[int, str] = field(default_factory=dict)
    train_class_counts: dict[int, int] = field(default_factory=dict)
    val_class_counts: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def _names(value: Any) -> dict[int, str]:
    if isinstance(value, list):
        return {i: str(name) for i, name in enumerate(value)}
    if isinstance(value, dict):
        try:
            names = {int(i): str(name) for i, name in value.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError("dataset names must use integer class IDs") from exc
        if sorted(names) != list(range(len(names))):
            raise ValueError("dataset class IDs must be contiguous and start at zero")
        return names
    raise ValueError("dataset YAML must contain a names list or mapping")


def _split_files(spec: Any, *, root: Path, yaml_dir: Path) -> list[Path]:
    entries = spec if isinstance(spec, list) else [spec]
    images: list[Path] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        path = Path(entry).expanduser()
        path = path if path.is_absolute() else root / path
        if path.is_dir():
            images.extend(
                p.resolve() for p in path.rglob("*") if p.suffix.lower() in _IMAGE_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() == ".txt":
            for raw in path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                image = Path(raw.strip()).expanduser()
                if not image.is_absolute():
                    rooted = root / image
                    image = rooted if rooted.exists() else yaml_dir / image
                images.append(image.resolve())
        elif path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
            images.append(path.resolve())
    return sorted(set(images))


def _label_path(image: Path) -> Path:
    parts = list(image.parts)
    positions = [i for i, part in enumerate(parts) if part == "images"]
    if positions:
        parts[positions[-1]] = "labels"
        return Path(*parts).with_suffix(".txt")
    return image.parent.parent / "labels" / image.parent.name / f"{image.stem}.txt"


def _audit_split(
    images: list[Path], names: dict[int, str], split: str, report: DatasetAudit
) -> tuple[dict[int, int], set[str]]:
    import cv2

    counts = {class_id: 0 for class_id in names}
    hashes: set[str] = set()
    instances = 0
    for image in images:
        if not image.is_file():
            report.errors.append(f"{split}: missing image {image}")
            continue
        frame = cv2.imread(str(image))
        if frame is None or frame.shape[0] < 2 or frame.shape[1] < 2:
            report.errors.append(f"{split}: unreadable image {image}")
            continue
        hashes.add(hashlib.sha256(image.read_bytes()).hexdigest())
        label = _label_path(image)
        if not label.is_file():
            report.errors.append(
                f"{split}: missing label {label} (use an empty file for a true negative)"
            )
            continue
        for line_number, raw in enumerate(label.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            fields = raw.split()
            if len(fields) != 5:
                report.errors.append(f"{label}:{line_number}: expected class + xywh")
                continue
            try:
                class_id = int(fields[0])
                box = [float(value) for value in fields[1:]]
            except ValueError:
                report.errors.append(f"{label}:{line_number}: non-numeric YOLO label")
                continue
            if class_id not in names:
                report.errors.append(f"{label}:{line_number}: unknown class ID {class_id}")
                continue
            if not all(math.isfinite(value) and 0 <= value <= 1 for value in box):
                report.errors.append(f"{label}:{line_number}: xywh must be finite and normalized")
                continue
            if box[2] <= 0 or box[3] <= 0:
                report.errors.append(f"{label}:{line_number}: box width/height must be positive")
                continue
            counts[class_id] += 1
            instances += 1
    if split == "train":
        report.train_instances = instances
    else:
        report.val_instances = instances
    return counts, hashes


def audit_yolo_dataset(data_yaml: str | Path) -> DatasetAudit:
    """Audit image integrity, YOLO labels, class balance, and train/val leakage."""

    path = Path(data_yaml).expanduser().resolve()
    report = DatasetAudit(data_yaml=str(path))
    if not path.is_file():
        report.errors.append(f"dataset YAML not found: {path}")
        return report
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names = _names(config.get("names"))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        report.errors.append(str(exc))
        return report
    report.class_names = names
    root_value = Path(str(config.get("path", "."))).expanduser()
    root = root_value if root_value.is_absolute() else path.parent / root_value
    root = root.resolve()
    train = _split_files(config.get("train"), root=root, yaml_dir=path.parent)
    val = _split_files(config.get("val"), root=root, yaml_dir=path.parent)
    report.train_images, report.val_images = len(train), len(val)
    if not train:
        report.errors.append("training split contains no images")
    if not val:
        report.errors.append("validation split contains no images")
    report.train_class_counts, train_hashes = _audit_split(train, names, "train", report)
    report.val_class_counts, val_hashes = _audit_split(val, names, "val", report)
    overlap = train_hashes & val_hashes
    if overlap:
        report.errors.append(f"train/val leakage: {len(overlap)} identical image(s)")
    if len(train) < 100:
        report.warnings.append(f"only {len(train)} training images; site adaptation is likely unstable")
    if len(val) < 50:
        report.warnings.append(f"only {len(val)} validation images; metrics will have high variance")
    for class_id, name in names.items():
        train_count = report.train_class_counts.get(class_id, 0)
        val_count = report.val_class_counts.get(class_id, 0)
        if train_count < 20:
            report.warnings.append(f"class {class_id} ({name}) has only {train_count} train boxes")
        if val_count < 10:
            report.warnings.append(f"class {class_id} ({name}) has only {val_count} val boxes")
    return report
