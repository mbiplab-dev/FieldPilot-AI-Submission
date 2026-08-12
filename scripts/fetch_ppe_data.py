#!/usr/bin/env python3
"""Fetch the two licensed public corpora used for FieldPilot PPE adaptation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

CSS_KAGGLE_ID = "snehilsanyal/construction-site-safety-image-dataset-roboflow"
CSS_URL = f"https://www.kaggle.com/datasets/{CSS_KAGGLE_ID}"
CPPE_URL = (
    "https://github.com/ultralytics/assets/releases/download/v0.0.0/"
    "construction-ppe.zip"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        corrupt = bundle.testzip()
        if corrupt:
            raise RuntimeError(f"corrupt ZIP member in {archive}: {corrupt}")
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(root):
                raise RuntimeError(f"unsafe ZIP path in {archive}: {member.filename}")
        bundle.extractall(destination)


def _download_construction_ppe(destination: Path) -> None:
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(CPPE_URL, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def fetch(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    css_archive = output / "construction-site-safety-image-dataset-roboflow.zip"
    cppe_archive = output / "construction-ppe.zip"

    if not css_archive.is_file():
        if shutil.which("kaggle") is None:
            raise RuntimeError(
                "Kaggle CLI is required. Install it and configure ~/.kaggle/kaggle.json."
            )
        subprocess.run(
            [
                "kaggle",
                "datasets",
                "download",
                "-d",
                CSS_KAGGLE_ID,
                "-p",
                str(output),
            ],
            check=True,
        )
    if not cppe_archive.is_file():
        _download_construction_ppe(cppe_archive)

    css_root = output / "css"
    cppe_root = output / "construction_ppe"
    if not (css_root / "css-data" / "train" / "images").is_dir():
        _safe_extract(css_archive, css_root)
    if not (cppe_root / "images" / "train").is_dir():
        _safe_extract(cppe_archive, cppe_root)

    manifest: dict[str, object] = {
        "retrieved_at": datetime.now(UTC).isoformat(),
        "sources": [
            {
                "key": "css",
                "title": "Construction Site Safety Image Dataset Roboflow",
                "url": CSS_URL,
                "license": "CC BY 4.0",
                "archive": css_archive.name,
                "sha256": _sha256(css_archive),
            },
            {
                "key": "construction_ppe",
                "title": "Ultralytics Construction-PPE",
                "url": "https://docs.ultralytics.com/datasets/detect/construction-ppe/",
                "license": "AGPL-3.0",
                "archive": cppe_archive.name,
                "sha256": _sha256(cppe_archive),
            },
        ],
    }
    (output / "SOURCE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/training_sources"))
    args = parser.parse_args()
    manifest = fetch(args.output.expanduser().resolve())
    for source in manifest["sources"]:
        print(f"ready: {source['title']} ({source['license']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
