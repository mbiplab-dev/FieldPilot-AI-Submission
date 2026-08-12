#!/usr/bin/env python3
"""Merge licensed PPE corpora into FieldPilot's stable ten-class YOLO schema."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

NAMES = [
    "Hardhat",
    "Mask",
    "NO-Hardhat",
    "NO-Mask",
    "NO-Safety Vest",
    "Person",
    "Safety Cone",
    "Safety Vest",
    "machinery",
    "vehicle",
]
SPLIT_PRIORITY = {"train": 0, "val": 1, "test": 2}
IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


@dataclass(frozen=True)
class Source:
    key: str
    root: Path
    splits: dict[str, str]
    class_map: dict[int, int]
    url: str
    license: str
    roboflow_groups: bool = False


@dataclass(frozen=True)
class Record:
    source: Source
    requested_split: str
    image: Path
    label: Path
    digest: str
    group: str


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_group(source: Source, image: Path) -> str:
    name = image.name
    if source.roboflow_groups:
        name = name.split(".rf.", 1)[0]
    return f"{source.key}:{name}"


def _records(source: Source) -> list[Record]:
    records: list[Record] = []
    for requested_split, source_split in source.splits.items():
        image_dir = source.root / source_split / "images"
        label_dir = source.root / source_split / "labels"
        if not image_dir.is_dir() and (source.root / "images" / source_split).is_dir():
            image_dir = source.root / "images" / source_split
            label_dir = source.root / "labels" / source_split
        if not image_dir.is_dir() or not label_dir.is_dir():
            raise FileNotFoundError(f"missing YOLO split below {source.root}: {source_split}")
        for image in sorted(image_dir.iterdir()):
            if image.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            label = label_dir / f"{image.stem}.txt"
            if not label.is_file():
                raise FileNotFoundError(f"missing label for {image}: {label}")
            records.append(
                Record(
                    source=source,
                    requested_split=requested_split,
                    image=image,
                    label=label,
                    digest=_sha256(image),
                    group=_source_group(source, image),
                )
            )
    return records


def _remapped_lines(record: Record) -> set[str]:
    lines: set[str] = set()
    for number, raw in enumerate(record.label.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        fields = raw.split()
        if len(fields) != 5:
            raise ValueError(f"{record.label}:{number}: expected class + xywh")
        try:
            source_class = int(fields[0])
        except ValueError as exc:
            raise ValueError(f"{record.label}:{number}: invalid class ID") from exc
        target_class = record.source.class_map.get(source_class)
        if target_class is not None:
            lines.add(" ".join([str(target_class), *fields[1:]]))
    return lines


def _link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def prepare(sources: list[Source], output: Path) -> dict[str, object]:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output is not empty: {output}; remove generated data explicitly")
    output.mkdir(parents=True, exist_ok=True)
    records = [record for source in sources for record in _records(source)]
    records_by_digest: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        records_by_digest[record.digest].append(record)
    union = UnionFind(len(records))
    first_group: dict[str, int] = {}
    first_digest: dict[str, int] = {}
    for index, record in enumerate(records):
        for key, table in ((record.group, first_group), (record.digest, first_digest)):
            if key in table:
                union.union(index, table[key])
            else:
                table[key] = index

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        components[union.find(index)].append(index)
    assigned_split = {
        root: max(
            (records[index].requested_split for index in indices),
            key=SPLIT_PRIORITY.__getitem__,
        )
        for root, indices in components.items()
    }

    for split in SPLIT_PRIORITY:
        (output / "images" / split).mkdir(parents=True, exist_ok=True)
        (output / "labels" / split).mkdir(parents=True, exist_ok=True)

    image_counts: Counter[str] = Counter()
    box_counts: dict[str, Counter[int]] = defaultdict(Counter)
    moved_for_leakage = 0
    seen_digests: set[str] = set()
    exact_duplicates = 0
    for index, record in enumerate(records):
        split = assigned_split[union.find(index)]
        if split != record.requested_split:
            moved_for_leakage += 1
        if record.digest in seen_digests:
            exact_duplicates += 1
            continue
        seen_digests.add(record.digest)
        label_lines: set[str] = set()
        for item in records_by_digest[record.digest]:
            label_lines.update(_remapped_lines(item))
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", record.image.stem)[:80]
        name = f"{record.source.key}_{record.digest[:12]}_{safe_stem}"
        image_destination = output / "images" / split / f"{name}{record.image.suffix.lower()}"
        label_destination = output / "labels" / split / f"{name}.txt"
        _link_or_copy(record.image, image_destination)
        label_destination.write_text(
            "\n".join(sorted(label_lines, key=lambda line: (int(line.split()[0]), line)))
            + ("\n" if label_lines else ""),
            encoding="utf-8",
        )
        image_counts[split] += 1
        for line in label_lines:
            box_counts[split][int(line.split()[0])] += 1

    data_yaml = {
        # Ultralytics resolves a relative `path` against its global datasets directory/current
        # process rather than reliably against this YAML file. This generated artifact is local,
        # so an absolute path is intentional and avoids silently training on the wrong directory.
        "path": str(output.resolve()),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {index: name for index, name in enumerate(NAMES)},
    }
    (output / "data.yaml").write_text(yaml.safe_dump(data_yaml, sort_keys=False), encoding="utf-8")
    manifest: dict[str, object] = {
        "created_at": datetime.now(UTC).isoformat(),
        "policy": (
            "Exact duplicates and Roboflow augmentations from one source frame are assigned to "
            "a single split; test takes precedence over validation, then training."
        ),
        "warning": (
            "Public validation is provisional. Promotion for site use requires a separate, "
            "session-split phone-camera validation set."
        ),
        "images": dict(image_counts),
        "boxes": {
            split: {NAMES[class_id]: count for class_id, count in sorted(counts.items())}
            for split, counts in box_counts.items()
        },
        "records_moved_to_prevent_split_leakage": moved_for_leakage,
        "exact_duplicate_files_removed": exact_duplicates,
        "sources": [
            {
                "key": source.key,
                "url": source.url,
                "license": source.license,
                "class_map": {
                    str(source_id): NAMES[target_id]
                    for source_id, target_id in source.class_map.items()
                },
            }
            for source in sources
        ],
    }
    (output / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def default_sources(root: Path) -> list[Source]:
    return [
        Source(
            key="css",
            root=root / "css" / "css-data",
            splits={"train": "train", "val": "valid", "test": "test"},
            class_map={index: index for index in range(len(NAMES))},
            url=(
                "https://www.kaggle.com/datasets/snehilsanyal/"
                "construction-site-safety-image-dataset-roboflow"
            ),
            license="CC BY 4.0",
            roboflow_groups=True,
        ),
        Source(
            key="construction_ppe",
            root=root / "construction_ppe",
            splits={"train": "train", "val": "val", "test": "test"},
            class_map={0: 0, 2: 7, 6: 5, 7: 2},
            url="https://docs.ultralytics.com/datasets/detect/construction-ppe/",
            license="AGPL-3.0",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("data/training_sources"))
    parser.add_argument("--output", type=Path, default=Path("data/training/ppe_combined"))
    args = parser.parse_args()
    manifest = prepare(
        default_sources(args.sources.expanduser().resolve()), args.output.expanduser().resolve()
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
