"""Selectable detector checkpoints: a pinned registry plus verified, atomic downloads.

Why a registry at all: `config.yaml: detection.ppe_model` names exactly one checkpoint, and
`models/*.pt` is gitignored, so a fresh clone has no weights and an operator who wants a different
label space has to go find one. This module makes that choice explicit and safe — every entry names
its licence, its capability, and (where it is downloadable) a URL pinned to an immutable revision
together with the SHA-256 the bytes must hash to.

Two properties are deliberate and load-bearing:

* **`capability`** is `"ppe"` or `"person"`. A person-only COCO detector cannot see hardhats, so
  running PPE compliance against one manufactures "missing PPE" for every worker on site. Callers
  gate PPE alerting on this field; it is not decoration.
* **Nothing unverified lands on disk.** Downloads stream into a `.part` sibling, stop dead if the
  response exceeds the entry's declared size, are hashed before `Path.replace()`, and the temp file
  is always removed. A digest mismatch — on a fresh download *or* on a file already in `models/` —
  deletes the file and raises. A checkpoint is executable-ish input to torch; a silently corrupt or
  substituted one is a supply-chain problem, not an inconvenience.

The module imports no ML packages. `ultralytics`/`torch` are pulled in lazily inside `load_model`
only, so a FastAPI process (or a test) can list and verify weights for the cost of a few `stat`s.
It also sets no environment variables at import time — that is an application's call, not a
library's.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import urllib.request
from pathlib import Path
from typing import Any

from fieldpilot.logging_.logger import get_logger

logger = get_logger("fieldpilot.models_registry")

# Repo-root `models/`, the directory `config.yaml` paths (`models/ppe_css.pt`) already resolve to.
DEFAULT_MODELS_DIR = Path(__file__).resolve().parents[2] / "models"

CAPABILITIES = frozenset({"ppe", "person"})
REQUIRED_FIELDS = ("weights", "label", "notes", "capability", "license")

_USER_AGENT = "fieldpilot-models-registry/1.0"
_TIMEOUT_S = 90
_CHUNK_BYTES = 1024 * 1024
# A truthful Content-Length cannot be assumed (it is attacker/mirror controlled), so the ceiling
# comes from the *registry's* declared size with headroom for re-packed checkpoints.
_SIZE_HEADROOM = 1.15
_SIZE_SLACK_BYTES = 1024
_DEFAULT_SIZE_MB = 100.0


class ModelRegistryError(RuntimeError):
    """A checkpoint could not be obtained, verified, or loaded. The message says which and why."""


# --------------------------------------------------------------------------- the registry
#
# PPE URLs pin a git revision (`/resolve/<40-hex>/`) rather than `/resolve/main/`: `main` is a
# mutable pointer, so pinning it would make the recorded SHA-256 meaningless the moment the
# uploader pushes a new commit. Digests below were taken from the published LFS metadata / the
# bytes actually on disk — never guessed.

MODEL_OPTIONS: dict[str, dict[str, Any]] = {
    "ppe_css": {
        "weights": "ppe_css.pt",
        "label": "Construction-Site Safety Nano · 10 classes",
        "notes": (
            "The default in config.yaml. Hardhat, Mask, NO-Hardhat, NO-Mask, NO-Safety Vest, "
            "Person, Safety Cone, Safety Vest, machinery, vehicle — the exact label space "
            "PPEChecker._categorize was written against."
        ),
        "capability": "ppe",
        "license": "MIT",
        "size_mb": 6.0,
        "source": "https://huggingface.co/Hansung-Cho/yolov8-ppe-detection",
        "url": (
            "https://huggingface.co/Hansung-Cho/yolov8-ppe-detection/resolve/"
            "ac0027bd38bc619d5ce4f52b4cc01beb87d8b958/best.pt"
        ),
        "sha256": "2419700bbe3b8d38f9000655d9cf952a4bc93ef6c143baf8b49a0abde5d0760f",
    },
    "ppe_construction_n": {
        "weights": "ppe_construction_n.pt",
        "label": "PPE Construction Nano · 17 classes",
        "notes": "Fast PPE model for helmets, vests, gloves, shoes, people, and site equipment.",
        "capability": "ppe",
        "license": "MIT",
        "size_mb": 6.0,
        "source": "https://huggingface.co/baskarmother/yolov8-ppe-construction",
        "url": (
            "https://huggingface.co/baskarmother/yolov8-ppe-construction/resolve/"
            "3213ed51de90cbc76e577e6944e84f7c74343526/best.pt"
        ),
        "sha256": "8714b4b2bbde95b3a07dcdbe873995e34742b5ce628464a4da232721d4691ffe",
    },
    "ppe_helmet_vest_n": {
        "weights": "ppe_helmet_vest_n.pt",
        "label": "PPE Helmet + Vest Nano · 5 classes",
        "notes": "YOLO11 model specialized for hats, missing hats, vests, missing vests, and people.",
        "capability": "ppe",
        "license": "Apache-2.0",
        "size_mb": 5.1,
        "source": "https://huggingface.co/wesjos/Yolo-hard-hat-safety-vest",
        "url": (
            "https://huggingface.co/wesjos/Yolo-hard-hat-safety-vest/resolve/"
            "44b1c59aa64f4039ef732c82563a9b30abdc991a/yolo11n_safety.pt"
        ),
        "sha256": "93c8ddf095891486590430edd3451525355e9b76b0be73543b5aa87435ce9a6e",
    },
    "ppe_safetyvision_s": {
        "weights": "ppe_safetyvision_s.pt",
        "label": "SafetyVision PPE Small · 13 classes",
        "notes": "Balanced YOLOv8s model for positive and missing PPE, people, masks, and falls.",
        "capability": "ppe",
        "license": "AGPL-3.0",
        "size_mb": 21.5,
        "source": "https://huggingface.co/ayushgupta7777/safetyvision-yolov8",
        "url": (
            "https://huggingface.co/ayushgupta7777/safetyvision-yolov8/resolve/"
            "56a71758b55f0e9f2b4b2d6b51a779a1f882da10/v2/best.pt"
        ),
        "sha256": "7863be4700dcf831579d610bb3fe3668fb29fb22ab17ca027b55e94b88bfff7a",
    },
    "ppe_vyra_m": {
        "weights": "ppe_vyra_m.pt",
        "label": "Vyra PPE Medium · 14 classes",
        "notes": "Higher-capacity YOLOv8m model for helmets, vests, gloves, goggles, masks, and falls.",
        "capability": "ppe",
        "license": "CC-BY-4.0",
        "size_mb": 49.7,
        "source": "https://huggingface.co/Hexmon/vyra-yolo-ppe-detection",
        "url": (
            "https://huggingface.co/Hexmon/vyra-yolo-ppe-detection/resolve/"
            "08895b33d95d2587423ebe4f7c1b9c41beebd642/best.pt"
        ),
        "sha256": "2b33d4d016f9751c5a25f4a72ce050e0a7e4e140b11c1669978d7154003a4f61",
    },
    # Person-only detectors: no URL and no digest on purpose. Ultralytics resolves these names from
    # its own release assets, so inventing a mirror URL would add a second, unpinned trust anchor.
    # `load_model` falls back to that auto-download; PPE alerting must stay off while one is active.
    "yolo26n": {
        "weights": "yolo26n.pt",
        "label": "YOLO26 Nano",
        "notes": "Fast general detector — people only, no PPE classes. Pose remains enabled.",
        "capability": "person",
        "license": "AGPL-3.0",
        "ultralytics_name": "yolo26n.pt",
    },
    "yolo26s": {
        "weights": "yolo26s.pt",
        "label": "YOLO26 Small",
        "notes": "Higher-accuracy general detector with slower CPU inference. People only.",
        "capability": "person",
        "license": "AGPL-3.0",
        "ultralytics_name": "yolo26s.pt",
    },
    "yolo11n": {
        "weights": "yolo11n.pt",
        "label": "YOLO11 Nano",
        "notes": "Stable fallback when YOLO26 weights are unavailable. People only.",
        "capability": "person",
        "license": "AGPL-3.0",
        "size_mb": 5.4,
        "ultralytics_name": "yolo11n.pt",
    },
    "custom": {
        "weights": "custom_ppe.pt",
        "label": "Custom PPE Model",
        "notes": "Your own fine-tune, dropped in as models/custom_ppe.pt. Never downloaded.",
        "capability": "ppe",
        "license": "User supplied",
    },
}


# --------------------------------------------------------------------------- paths + digests

def _models_dir(models_dir: str | Path | None) -> Path:
    return Path(models_dir).expanduser() if models_dir is not None else DEFAULT_MODELS_DIR


def _require(key: str) -> dict[str, Any]:
    option = MODEL_OPTIONS.get(key)
    if option is None:
        raise ModelRegistryError(
            f"Unknown model key {key!r}. Available: {', '.join(sorted(MODEL_OPTIONS))}."
        )
    return option


def file_sha256(path: str | Path) -> str:
    """SHA-256 of a file, streamed so a 50 MB checkpoint never lands in memory whole."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def weights_path(key: str, models_dir: str | Path | None = None) -> Path:
    """Where `key`'s checkpoint lives (whether or not it is there yet)."""

    return _models_dir(models_dir) / _require(key)["weights"]


# --------------------------------------------------------------------------- read-only queries

def get_option(key: str) -> dict[str, Any] | None:
    """The registry entry for `key`, or None. A copy — callers cannot mutate the registry."""

    option = MODEL_OPTIONS.get(key)
    return copy.deepcopy(option) if option is not None else None


def list_models(models_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Every entry, annotated with `key`, `path`, `downloaded`, and on-disk `size_bytes`.

    Cheap by design (a `stat` per entry, no hashing), so a FastAPI handler can call it inline.
    Use `verify_local` when the answer has to be "and the bytes are the right bytes".
    """

    root = _models_dir(models_dir)
    entries: list[dict[str, Any]] = []
    for key, option in MODEL_OPTIONS.items():
        path = root / option["weights"]
        present = path.is_file()
        entries.append(
            {
                "key": key,
                **copy.deepcopy(option),
                "path": str(path),
                "downloaded": present,
                "size_bytes": path.stat().st_size if present else None,
            }
        )
    return entries


def verify_local_sync(key: str, models_dir: str | Path | None = None) -> tuple[bool, str]:
    """(ok, detail): is the checkpoint present, and does it hash to the pinned digest?"""

    option = _require(key)
    path = weights_path(key, models_dir)
    if not path.is_file():
        return False, f"{path} does not exist"
    expected = option.get("sha256")
    if not expected:
        return True, f"present ({path.stat().st_size} bytes); no pinned digest to check against"
    actual = file_sha256(path)
    if actual != expected:
        return False, f"digest mismatch: expected {expected}, found {actual}"
    return True, f"present and verified against pinned sha256 {expected}"


async def verify_local(key: str, models_dir: str | Path | None = None) -> tuple[bool, str]:
    """Async `verify_local_sync` — hashing is offloaded so it cannot block the event loop."""

    return await asyncio.to_thread(verify_local_sync, key, models_dir)


# --------------------------------------------------------------------------- verified download

def _urlopen(request: urllib.request.Request, timeout: int = _TIMEOUT_S) -> Any:
    """Single seam for network I/O — tests monkeypatch this instead of the whole urllib module."""

    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 — https URLs from the registry


def _download_verified(option: dict[str, Any], destination: Path) -> None:
    """Stream a pinned checkpoint to `destination`, atomically and only if it verifies.

    Guarantees, in order: bytes go to a `.part` sibling; the transfer aborts if it exceeds the
    declared size (a runaway or hostile response cannot fill the disk); the digest is checked
    *before* `replace()`; the temp file is removed on every path. `destination` therefore either
    holds verified bytes or does not exist.
    """

    url = option["url"]
    expected = option.get("sha256")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    ceiling = int(float(option.get("size_mb", _DEFAULT_SIZE_MB)) * 1024 * 1024 * _SIZE_HEADROOM)
    ceiling += _SIZE_SLACK_BYTES

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    logger.info("downloading %s -> %s", url, destination)
    try:
        total = 0
        with _urlopen(request, timeout=_TIMEOUT_S) as response, part.open("wb") as fh:
            while chunk := response.read(_CHUNK_BYTES):
                total += len(chunk)
                if total > ceiling:
                    raise ModelRegistryError(
                        f"{option['label']}: download exceeded its declared size "
                        f"({total} bytes > {ceiling} ceiling for {option.get('size_mb')} MB) — "
                        "aborted. The URL is serving something other than the pinned checkpoint."
                    )
                fh.write(chunk)
        if expected:
            actual = file_sha256(part)
            if actual != expected:
                raise ModelRegistryError(
                    f"{option['label']}: checksum verification failed (expected {expected}, "
                    f"got {actual}). The download was discarded; nothing was installed."
                )
        part.replace(destination)
        logger.info("installed %s (%d bytes, digest verified: %s)", destination, total, bool(expected))
    finally:
        part.unlink(missing_ok=True)


def _unobtainable(key: str, option: dict[str, Any], path: Path) -> ModelRegistryError:
    """The 'it is not here and I will not invent it' error, phrased per entry."""

    if key == "custom":
        return ModelRegistryError(
            f"Custom PPE weights are not present at {path}. This slot is never downloaded — copy "
            "your fine-tuned YOLO checkpoint there (any detection model works; PPE classes are "
            "matched by name, e.g. Hardhat / NO-Hardhat / Safety Vest / NO-Safety Vest)."
        )
    if option.get("ultralytics_name"):
        return ModelRegistryError(
            f"{option['label']} is not at {path} and the registry pins no URL for it: Ultralytics "
            f"resolves {option['ultralytics_name']!r} from its own release assets. Use "
            "`load_model` (which falls back to that auto-download) or run "
            "`uv run python scripts/fetch_models.py`."
        )
    return ModelRegistryError(f"{option['label']} is not at {path} and has no download URL.")


def ensure_weights_sync(
    key: str, models_dir: str | Path | None = None, *, force: bool = False
) -> Path:
    """Return a verified local path for `key`, downloading it only if needed.

    Idempotent: a present file whose digest matches the pin is returned untouched. A present file
    whose digest does *not* match is deleted before any retry — an unverifiable checkpoint is not
    something to keep "just in case", and leaving it would let a stale mismatch silently load.
    """

    option = _require(key)
    path = weights_path(key, models_dir)
    expected = option.get("sha256")

    if path.is_file() and not force:
        if not expected:
            return path
        actual = file_sha256(path)
        if actual == expected:
            return path
        logger.warning(
            "%s at %s does not match its pinned digest (expected %s, found %s) — deleting it",
            option["label"], path, expected, actual,
        )
        path.unlink(missing_ok=True)

    if not option.get("url"):
        raise _unobtainable(key, option, path)

    _download_verified(option, path)
    return path


async def ensure_weights(
    key: str, models_dir: str | Path | None = None, *, force: bool = False
) -> Path:
    """Async `ensure_weights_sync` — download + hashing run off the event loop."""

    return await asyncio.to_thread(ensure_weights_sync, key, models_dir, force=force)


# --------------------------------------------------------------------------- optional loading

def load_model_sync(key: str, models_dir: str | Path | None = None) -> Any:
    """Ensure the weights, then build an `ultralytics.YOLO`. Imports torch — call off-thread.

    `ultralytics` is imported *here*, never at module scope: the registry has to stay importable
    (and fast) in processes and tests that will never run inference.
    """

    option = _require(key)
    try:
        path: Path | str = ensure_weights_sync(key, models_dir)
    except ModelRegistryError:
        # Ultralytics-hosted names are the one case where "no local file" is still recoverable.
        if not option.get("ultralytics_name"):
            raise
        path = option["ultralytics_name"]
        logger.info("%s: falling back to ultralytics auto-download of %s", key, path)

    from ultralytics import YOLO  # noqa: PLC0415 — deliberately lazy: pulls torch

    try:
        return YOLO(str(path))
    except Exception as exc:  # noqa: BLE001 — any load failure is "unusable", and we say why
        raise ModelRegistryError(f"Could not load {option['label']} from {path}: {exc}") from exc


async def load_model(key: str, models_dir: str | Path | None = None) -> Any:
    """Async `load_model_sync`."""

    return await asyncio.to_thread(load_model_sync, key, models_dir)
