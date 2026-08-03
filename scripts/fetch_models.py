#!/usr/bin/env python
"""Fetch the model weights FieldPilot needs into `models/`.

`models/*.pt` is gitignored, so a fresh clone has no weights and `detection.ppe_model` points at a
file that does not exist. This script closes that gap:

    uv run python scripts/fetch_models.py              # everything that is missing
    uv run python scripts/fetch_models.py --only ppe   # just the PPE detector
    uv run python scripts/fetch_models.py --force      # redownload even if present

It is idempotent (a model that is present and verifies is skipped), it verifies every file it puts
on disk (plausible size, torch/zip magic, actually loads with `ultralytics.YOLO`) and it prints the
class names it found so you can see the label space you got. Where no reachable, correctly-licensed
public weight file exists, it prints manual instructions and exits non-zero for *that model only*
rather than inventing a URL — see DAMAGE below.

Exit codes: 0 = every requested model is present and verified; 1 = at least one is not.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_DIR = REPO_ROOT / "models"
_USER_AGENT = "fieldpilot-fetch-models/1.0"


@dataclass(frozen=True)
class ModelSpec:
    key: str                       # --only selector
    filename: str                  # name inside models/
    what: str                      # one-line description
    min_bytes: int                 # a smaller file is an error page, not a checkpoint
    urls: tuple[str, ...] = ()     # tried in order; empty => manual-only
    source: str = ""               # human-readable provenance
    license: str = ""
    ultralytics_name: str | None = None  # fallback: let ultralytics auto-download this name
    manual: tuple[str, ...] = field(default_factory=tuple)  # printed when we cannot fetch it


# --- Pose backbone ------------------------------------------------------------------------------
# Official Ultralytics release assets (AGPL-3.0, same licence as the ultralytics package this
# project already depends on). Verified reachable: HTTP 200, 42,459,307 bytes.
POSE = ModelSpec(
    key="pose",
    filename="yolo11m-pose.pt",
    what="YOLO11-medium pose backbone (person keypoints — the core of the safety loop)",
    min_bytes=20_000_000,
    urls=(
        "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11m-pose.pt",
    ),
    source="github.com/ultralytics/assets release v8.3.0",
    license="AGPL-3.0 (Ultralytics)",
    ultralytics_name="yolo11m-pose.pt",
)

# --- PPE detector -------------------------------------------------------------------------------
# Hansung-Cho/yolov8-ppe-detection on the Hugging Face Hub: a YOLOv8n fine-tune on public
# construction-site-safety data, MIT-licensed, reported mAP@0.50 = 0.744.
#   https://huggingface.co/Hansung-Cho/yolov8-ppe-detection
# Chosen because its label space is *exactly* the 10-class "construction site safety" set this
# codebase was written against — Hardhat, Mask, NO-Hardhat, NO-Mask, NO-Safety Vest, Person,
# Safety Cone, Safety Vest, machinery, vehicle — which `PPEChecker._categorize` maps directly to
# helmet/vest compliance, and whose machinery/vehicle/cone classes feed `equipment_boxes`.
# Verified by download: HTTP 200, 6,250,090 bytes, loads with ultralytics 8.4.x, 10 names as above.
# It lands as `ppe_css.pt` because that is what `config.yaml: detection.ppe_model` points at.
PPE = ModelSpec(
    key="ppe",
    filename="ppe_css.pt",
    what="10-class construction PPE detector (Hardhat/Safety Vest + their NO- violations)",
    min_bytes=2_000_000,
    urls=(
        "https://huggingface.co/Hansung-Cho/yolov8-ppe-detection/resolve/main/best.pt",
    ),
    source="huggingface.co/Hansung-Cho/yolov8-ppe-detection (best.pt)",
    license="MIT",
    manual=(
        "Any YOLO detection model works — point `detection.ppe_model` at it in config.yaml.",
        "PPEChecker._categorize keys off class *names*, so the model must expose compliance and",
        "violation classes it can recognise, e.g. the 10-class construction-site-safety set:",
        "  Hardhat, Mask, NO-Hardhat, NO-Mask, NO-Safety Vest, Person, Safety Cone, Safety Vest,",
        "  machinery, vehicle",
        "Naming rules: a name containing 'hardhat'/'helmet' is treated as a helmet class, one",
        "containing 'vest' as a vest class, and a 'NO-' / 'no_' / 'no ' prefix marks it a",
        "violation. 'machinery'/'excavator'/'truck'/'vehicle'/'cone' names feed the equipment",
        "overlay. Alternatives: train on the Roboflow 'Construction Site Safety' dataset, or",
        "search the Hugging Face Hub for 'ppe yolo' and check the licence before use.",
    ),
)

# --- Structural-damage detector -----------------------------------------------------------------
# NO PUBLIC SOURCE. `models/structural_damage_best.pt` is a project-specific fine-tune with the
# 3 classes Minorrotation / Moderaterotation / Severerotation. We deliberately do NOT guess a URL
# for it: inspection mode is off by default (`inspection.enabled: false`), so a missing damage
# model costs nothing until someone turns it on.
DAMAGE = ModelSpec(
    key="damage",
    filename="structural_damage_best.pt",
    what="structural-damage / crack detector for inspection mode (3 severity classes)",
    min_bytes=1_000_000,
    urls=(),
    source="no public download — team-trained weights",
    license="internal",
    manual=(
        "These weights are a FieldPilot fine-tune with no public mirror; there is no URL to",
        "download. Options:",
        "  1. Copy structural_damage_best.pt from a teammate / your artefact store into models/.",
        "  2. Train your own: any YOLO detection fine-tune works. The dashboard expects severity-",
        "     ranked class names — 'Minorrotation', 'Moderaterotation', 'Severerotation' — and",
        "     `inspection.model` in config.yaml can point anywhere.",
        "  3. Leave it out. `inspection.enabled: false` is the default, so the safety loop and",
        "     PPE detection are unaffected; only inspection mode stays unavailable.",
    ),
)

SPECS: tuple[ModelSpec, ...] = (POSE, PPE, DAMAGE)


# ---------------------------------------------------------------------------- output helpers

def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _progress(done: int, total: int, *, tty: bool) -> None:
    if not tty:
        return
    if total > 0:
        pct = min(100.0, done * 100.0 / total)
        bar = "#" * int(pct // 4)
        print(f"\r    [{bar:<25}] {pct:5.1f}%  {_human(done)} / {_human(total)}",
              end="", flush=True)
    else:
        print(f"\r    downloaded {_human(done)}", end="", flush=True)


# ---------------------------------------------------------------------------- verification

def verify_checkpoint(path: Path, min_bytes: int) -> tuple[bool, str, dict[int, str]]:
    """Is `path` a plausible, loadable YOLO checkpoint? Returns (ok, detail, class_names)."""

    if not path.is_file():
        return False, "file does not exist", {}
    size = path.stat().st_size
    if size < min_bytes:
        return False, f"implausibly small ({_human(size)} < {_human(min_bytes)}) — truncated " \
                      "download or an HTML error page saved as .pt", {}
    with path.open("rb") as fh:
        magic = fh.read(4)
    # torch.save writes a zip archive; anything else here is not a checkpoint.
    if not magic.startswith(b"PK"):
        return False, f"not a torch checkpoint (leading bytes {magic!r}, expected a zip archive)", {}
    try:
        from ultralytics import YOLO

        model = YOLO(str(path))
        names = {int(k): str(v) for k, v in model.names.items()}
    except Exception as exc:  # noqa: BLE001 — any load failure means "unusable", and we say why.
        return False, f"ultralytics could not load it ({type(exc).__name__}: {exc})", {}
    if not names:
        return False, "loaded but exposes no class names", {}
    return True, f"{_human(size)}, {len(names)} classes", names


# ---------------------------------------------------------------------------- downloading

def _download(url: str, dest: Path, *, tty: bool) -> None:
    """Stream `url` to `dest` atomically (via .part), showing progress. Raises on failure."""

    part = dest.with_suffix(dest.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 18):
                out.write(chunk)
                done += len(chunk)
                _progress(done, total, tty=tty)
        if tty:
            print(flush=True)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _ultralytics_autodownload(name: str, dest: Path) -> None:
    """Fallback: let ultralytics resolve `name` from its own release assets, then move it."""

    from ultralytics import YOLO

    YOLO(name)  # downloads into the ultralytics weights dir or cwd
    for candidate in (Path.cwd() / name, dest.parent / name):
        if candidate.is_file():
            if candidate != dest:
                shutil.move(str(candidate), str(dest))
            return
    raise FileNotFoundError(f"ultralytics reported success but {name} is nowhere on disk")


# ---------------------------------------------------------------------------- per-model driver

def ensure(spec: ModelSpec, models_dir: Path, *, force: bool, tty: bool) -> bool:
    dest = models_dir / spec.filename
    _say(f"\n{spec.key}: {spec.what}")
    _say(f"  target : {dest}")

    if dest.is_file() and not force:
        ok, detail, names = verify_checkpoint(dest, spec.min_bytes)
        if ok:
            _say(f"  status : PRESENT, verified ({detail})")
            _say(f"  classes: {', '.join(names[i] for i in sorted(names))}")
            return True
        _say(f"  status : present but UNUSABLE — {detail}")
        _say("           redownloading (use --force to always redownload)")

    if not spec.urls:
        _say("  status : MISSING — no public download exists for this model")
        _say(f"  source : {spec.source}")
        for line in spec.manual:
            _say(f"    {line}")
        return False

    _say(f"  source : {spec.source}  [licence: {spec.license}]")
    errors: list[str] = []
    for url in spec.urls:
        _say(f"  fetch  : {url}")
        try:
            _download(url, dest, tty=tty)
            break
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")
            _say(f"           failed: {type(exc).__name__}: {exc}")
    else:
        if spec.ultralytics_name:
            _say(f"  fetch  : ultralytics auto-download ({spec.ultralytics_name})")
            try:
                _ultralytics_autodownload(spec.ultralytics_name, dest)
            except Exception as exc:  # noqa: BLE001 — report, do not crash the other models.
                errors.append(f"ultralytics auto-download -> {type(exc).__name__}: {exc}")
                _say(f"           failed: {type(exc).__name__}: {exc}")

    if not dest.is_file():
        _say("  status : FAILED — could not obtain the file")
        for err in errors:
            _say(f"           {err}")
        for line in spec.manual:
            _say(f"    {line}")
        return False

    ok, detail, names = verify_checkpoint(dest, spec.min_bytes)
    if not ok:
        _say(f"  status : FAILED verification — {detail}")
        _say(f"           removing {dest}")
        dest.unlink(missing_ok=True)
        return False
    _say(f"  status : DOWNLOADED, verified ({detail})")
    _say(f"  classes: {', '.join(names[i] for i in sorted(names))}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download/verify the model weights FieldPilot needs into models/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only", action="append", choices=[s.key for s in SPECS], metavar="pose|ppe|damage",
        help="fetch just this model (repeatable); default: all of them",
    )
    parser.add_argument("--force", action="store_true", help="redownload even if already present")
    parser.add_argument(
        "--models-dir", default=str(DEFAULT_MODELS_DIR), help="destination directory"
    )
    args = parser.parse_args(argv)

    selected = [s for s in SPECS if not args.only or s.key in args.only]
    models_dir = Path(args.models_dir).expanduser().resolve()
    models_dir.mkdir(parents=True, exist_ok=True)
    tty = sys.stdout.isatty()

    _say(f"FieldPilot model fetch → {models_dir}")
    _say(f"requested: {', '.join(s.key for s in selected)}")

    results = {s.key: ensure(s, models_dir, force=args.force, tty=tty) for s in selected}

    _say("\n" + "-" * 72)
    for key, ok in results.items():
        _say(f"  {key:<7} {'OK' if ok else 'NOT AVAILABLE'}")
    missing = [k for k, ok in results.items() if not ok]
    if missing:
        _say(f"\n{len(missing)} of {len(results)} model(s) unavailable: {', '.join(missing)}")
        _say("See the instructions above — nothing was faked, those files are genuinely not here.")
        return 1
    _say("\nAll requested models are present and loadable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
