"""PPE checking (hard hat / hi-vis vest / gloves / boots / goggles) via a dedicated detector.

YOLO-Pose only yields person keypoints — it does NOT detect PPE — so PPE uses a *separate* object
detector. Violation boxes are associated to the nearest tracked worker and raised as PPE_MISSING
events; every PPE box is also exposed via `last_boxes` so the GUI can draw compliant (green) and
violation (red) detections on the live feed.

Pluggable: point `detection.ppe_model` at any YOLO PPE model, or set it null to disable cleanly.
"Any" is meant literally — the public PPE checkpoints disagree wildly on spelling ("Hardhat",
"NO-Hardhat", "no_hardhat", "hard hat", "Safety Vest", "safety_shoes"), so class names are pushed
through `normalise_label()` (punctuation/case/separator insensitive + an alias table) onto one
stable vocabulary: helmet / vest / gloves / boots / goggles and their `no_*` counterparts.

Three rules keep the alerts honest:

* **Capability gate.** `ppe_capable` is computed by intersecting the loaded model's class names with
  the PPE vocabulary. A COCO/person-only detector (e.g. `yolo26n`) cannot possibly evidence a
  missing helmet, so when it is loaded PPE violations are suppressed *entirely* — inventing them
  would be a fabricated safety alert. The detector still runs, because its machinery/vehicle boxes
  feed proximity monitoring.
* **Absence is only evidence for the big items.** Some datasets ship a positive `vest` class with no
  explicit `no_vest`. Then, and only then, a worker with no vest box centred inside their body box
  is reported as `no_vest` (same for `helmet`). Gloves/boots/goggles are too small and too often
  occluded for "not detected" to mean "not worn", so no absence rule is applied to them.
* **Per-item opt-in.** `safety.tracked_items` in config.yaml is the boot default; the operator can
  retune it at runtime via `set_tracked_items()`.

Failure is *loud*. A missing or unloadable weights file used to disable PPE silently, so a fresh
clone shipped a safety loop with no hardhat/vest alerts and nothing said so. Now: "not configured"
is an INFO (a legitimate choice), "configured but unloadable" is a WARNING naming the path, the
cause, the remedy and the consequence, and `describe()` exposes that reason so a health endpoint
can surface it. What has *not* changed is that PPE never takes down the safety loop — every failure
is contained here and the rest of the pipeline keeps running.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from fieldpilot.core.types import (
    FrameResult,
    HazardEvent,
    HazardType,
    PersonDetection,
    Severity,
)
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.safety.ppe")

_CONSEQUENCE = "PPE violation detection is DISABLED — no hardhat/vest alerts will fire"
_REMEDY = (
    "run `make fetch-models` to download a construction-PPE detector, or set "
    "`detection.ppe_model: null` in config.yaml to disable PPE deliberately"
)

# ----------------------------------------------------------------- label vocabulary

#: Every PPE item this checker understands. Also the valid keys for `set_tracked_items()`.
TRACKED_ITEMS: tuple[str, ...] = ("helmet", "vest", "gloves", "boots", "goggles")

#: item -> the canonical class name that *evidences* the violation.
MISSING_LABELS: dict[str, str] = {
    "helmet": "no_helmet",
    "vest": "no_vest",
    "gloves": "no_gloves",
    "boots": "no_boots",
    "goggles": "no_goggles",
}
_ITEM_BY_MISSING: dict[str, str] = {missing: item for item, missing in MISSING_LABELS.items()}
_ITEMS: frozenset[str] = frozenset(TRACKED_ITEMS)

#: The stable vocabulary. A model whose class names normalise into this set is `ppe_capable`.
PPE_CLASS_NAMES: frozenset[str] = frozenset(TRACKED_ITEMS) | frozenset(MISSING_LABELS.values())

#: Dataset spelling -> our vocabulary. Keys are already `normalise_label()`-shaped (lowercase,
#: `_`-separated), so "NO-Hardhat", "no hardhat" and "no_hardhat" all resolve through one entry.
CLASS_ALIASES: dict[str, str] = {
    # helmet
    "hardhat": "helmet",
    "hardhats": "helmet",
    "hard_hat": "helmet",
    "safety_helmet": "helmet",
    "hat": "helmet",
    "helmet_on": "helmet",
    "no_hardhat": "no_helmet",
    "nohardhat": "no_helmet",
    "no_hard_hat": "no_helmet",
    "no_hat": "no_helmet",
    "nohat": "no_helmet",
    "nohelmet": "no_helmet",
    "helmet_off": "no_helmet",
    "without_helmet": "no_helmet",
    "without_hardhat": "no_helmet",
    # vest
    "safety_vest": "vest",
    "high_vis_vest": "vest",
    "high_visibility_vest": "vest",
    "hi_vis_vest": "vest",
    "reflective_vest": "vest",
    "jacket_on": "vest",
    "no_safety_vest": "no_vest",
    "novest": "no_vest",
    "jacket_off": "no_vest",
    "without_vest": "no_vest",
    "without_safety_vest": "no_vest",
    # gloves
    "glove": "gloves",
    "safety_glove": "gloves",
    "safety_gloves": "gloves",
    "no_glove": "no_gloves",
    "noglove": "no_gloves",
    "nogloves": "no_gloves",
    "without_gloves": "no_gloves",
    # boots
    "boot": "boots",
    "safety_boot": "boots",
    "safety_boots": "boots",
    "safety_shoe": "boots",
    "safety_shoes": "boots",
    "no_boot": "no_boots",
    "noboots": "no_boots",
    "no_safety_shoes": "no_boots",
    "without_boots": "no_boots",
    # goggles
    "goggle": "goggles",
    "safety_goggles": "goggles",
    "safety_glasses": "goggles",
    "protective_glasses": "goggles",
    # hrm canonicalised goggles' negative as "no_goggle"; we keep it plural for symmetry with the
    # positive class and alias the singular in.
    "no_goggle": "no_goggles",
    "nogoggles": "no_goggles",
    "without_goggles": "no_goggles",
    # people
    "worker": "person",
    "workers": "person",
    "people": "person",
}

# Fuzzy fallback for spellings nobody has catalogued yet ("Vest-Worn", "helmet_missing_worker").
# It only ever fires when the alias table did not resolve the label into the vocabulary.
_ITEM_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("helmet", ("hardhat", "helmet")),
    ("vest", ("vest",)),
    ("gloves", ("glove",)),
    ("boots", ("boot", "safety_shoe")),
    ("goggles", ("goggle", "safety_glass")),
)
# "hat" must match as a whole token so "hatch" is not a hard hat.
_HAT_TOKENS: frozenset[str] = frozenset({"hat", "hats"})
_NEGATIVE_TOKENS: frozenset[str] = frozenset({"no", "non", "not", "without", "missing", "off"})

#: Items whose *absence* is trustworthy evidence of a violation (see module docstring). Used only
#: when the model has the positive class but no `no_*` class for the item.
INFERRABLE_ITEMS: tuple[str, ...] = ("helmet", "vest")

_ITEM_PHRASE: dict[str, str] = {
    "helmet": "a hard hat",
    "vest": "a safety vest",
    "gloves": "safety gloves",
    "boots": "safety boots",
    "goggles": "safety goggles",
}

_MACHINERY_KEYWORDS = (
    "machinery", "machine", "excavator", "loader", "crane", "bulldozer", "truck", "vehicle",
)


def normalise_label(label: object) -> str:
    """Map a detector class name onto our stable vocabulary.

    Punctuation, case and separators are irrelevant: "NO-Hardhat", "no hardhat" and "no_hardhat"
    all become ``no_helmet``. Unknown labels are returned in normalised form, never dropped.
    """

    value = re.sub(r"[^a-z0-9]+", "_", str(label).strip().lower()).strip("_")
    return CLASS_ALIASES.get(value, value)


def classify_label(label: object) -> tuple[str | None, bool]:
    """``(item, compliant)`` for a raw class name; ``(None, True)`` if it is not a PPE class.

    ``item`` is one of `TRACKED_ITEMS`; ``compliant`` is False for the ``no_*`` forms.
    """

    norm = normalise_label(label)
    if norm in _ITEM_BY_MISSING:
        return _ITEM_BY_MISSING[norm], False
    if norm in _ITEMS:
        return norm, True

    tokens = norm.split("_")
    item: str | None = None
    if _HAT_TOKENS & set(tokens):
        item = "helmet"
    else:
        for name, keywords in _ITEM_KEYWORDS:
            if any(keyword in norm for keyword in keywords):
                item = name
                break
    if item is None:
        return None, True
    negative = tokens[0] in _NEGATIVE_TOKENS or norm.startswith("no")
    return item, not negative


def canonical_labels(names: Iterable[object]) -> frozenset[str]:
    """The PPE vocabulary labels a detector's class names can actually evidence.

    This is the capability test: an empty result means the model knows nothing about PPE.
    """

    out: set[str] = set()
    for raw in names:
        item, compliant = classify_label(raw)
        if item is not None:
            out.add(item if compliant else MISSING_LABELS[item])
    return frozenset(out)


def center_inside(inner: Iterable[float], outer: Iterable[float]) -> bool:
    """True when the centre of the `inner` xyxy box lies inside the `outer` xyxy box."""

    ix1, iy1, ix2, iy2 = (float(v) for v in inner)
    ox1, oy1, ox2, oy2 = (float(v) for v in outer)
    cx, cy = (ix1 + ix2) / 2.0, (iy1 + iy2) / 2.0
    return ox1 <= cx <= ox2 and oy1 <= cy <= oy2


def _equipment_kind(raw_label: str) -> str | None:
    """'machinery' | 'vehicle' | 'cone' for the non-PPE classes proximity monitoring consumes."""

    nl = raw_label.lower()
    if any(keyword in nl for keyword in _MACHINERY_KEYWORDS):
        return "vehicle" if ("vehicle" in nl or "truck" in nl) else "machinery"
    return "cone" if "cone" in nl else None


def _coerce_items(names: Iterable[object], *, source: str) -> frozenset[str]:
    """Normalise operator-supplied item names; unknown names are dropped with a WARNING."""

    kept: set[str] = set()
    unknown: list[str] = []
    for name in names:
        candidate = normalise_label(name)
        item = candidate if candidate in _ITEMS else _ITEM_BY_MISSING.get(candidate)
        if item is None:
            unknown.append(str(name))
        else:
            kept.add(item)
    if unknown:
        log.warning(
            "ignoring unknown PPE item(s) %s from %s — valid items are %s",
            ", ".join(unknown), source, ", ".join(TRACKED_ITEMS),
        )
    return frozenset(kept)


def _tracked_from_config(cfg) -> frozenset[str]:
    """Boot default for the tracked items: `safety.tracked_items` if present, else everything."""

    raw = cfg.get("safety.tracked_items")
    if raw is None:
        return frozenset(TRACKED_ITEMS)
    if isinstance(raw, Mapping):
        names: list[object] = [key for key, on in raw.items() if on]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        names = list(raw)
    elif isinstance(raw, str):
        names = [raw]
    else:
        log.warning(
            "safety.tracked_items is %s, expected a mapping or a list — tracking every PPE item",
            type(raw).__name__,
        )
        return frozenset(TRACKED_ITEMS)
    tracked = _coerce_items(names, source="config safety.tracked_items")
    if not tracked:
        # An explicitly empty selection is legal but consequential, so it is stated out loud.
        log.warning("safety.tracked_items enables no PPE item — no PPE violations will be raised")
    return tracked


class PPEChecker:
    """PPE violation detection over a per-frame `FrameResult`.

    Public surface consumed elsewhere in the tree:

    * `enabled`, `status` / `describe()` — is the detector loaded, and if not, why not.
    * `ppe_capable`, `class_labels`, `inferrable_items` — what the loaded label space supports.
    * `tracked_items`, `set_tracked_items()` — per-item enable/disable, retunable at runtime.
    * `last_boxes` (`{label, bbox, cat, ok, ...}`), `equipment_boxes` (`{label, bbox, kind}`).
    * `update(result)` — inference + rules; `evaluate(...)` — the rules alone, model-free.
    """

    def __init__(self, cfg):
        self.enabled = False
        self._model_path: str | None = None
        self._reason: str | None = None
        self.cooldown_s = float(cfg.get("alerts.cooldown_s.ppe_missing", 20))
        self.conf_min = float(cfg.get("detection.conf_min", 0.35))
        self._model = None
        self._names: dict[int, str] = {}
        self._device = None
        # Cooldowns are keyed on (track_id, item) so a missing helmet never suppresses the alert
        # for that same worker's missing gloves.
        self._last_alert: dict[tuple, float] = {}
        self.last_boxes: list[dict] = []       # {label, bbox, cat, ok, ...} PPE boxes for overlay
        self.equipment_boxes: list[dict] = []  # {label, bbox, kind} machinery/vehicle/cone
        self._tracked: frozenset[str] = _tracked_from_config(cfg)
        self._class_labels: frozenset[str] = frozenset()
        self._ppe_capable = False
        self._inferrable: tuple[str, ...] = ()
        self._logged_failures: set[str] = set()
        model_path = cfg.get("detection.ppe_model")
        device = cfg.get("detection.device", "auto")
        if model_path:
            self._model_path = str(model_path)
            self._load(self._model_path, device)
        else:
            # Not a misconfiguration: null is the documented way to run without PPE.
            self._reason = f"detection.ppe_model is not configured — {_CONSEQUENCE}"
            log.info("PPE checker off: %s", self._reason)

    # -- status ----------------------------------------------------------------

    @property
    def status(self) -> dict[str, object]:
        """Read-only snapshot of why PPE is on or off, for /health-style reporting.

        Deliberately frozen at three keys — it is a published contract. Everything about the
        *label space* (capability, tracked items) lives in `describe(full=True)`.
        """

        return {"enabled": self.enabled, "model": self._model_path, "reason": self._reason}

    def describe(self, *, full: bool = False) -> dict[str, object]:
        """`status` as a fresh dict, so callers cannot mutate our state.

        `full=True` adds the label-space view a config/REST endpoint needs: `ppe_capable`,
        `class_labels`, `tracked_items` and `inferrable_items`.
        """

        snapshot = self.status
        if full:
            snapshot.update(
                {
                    "ppe_capable": self.ppe_capable,
                    "class_labels": sorted(self._class_labels),
                    "tracked_items": sorted(self._tracked),
                    "inferrable_items": list(self._inferrable),
                }
            )
        return snapshot

    @property
    def ppe_capable(self) -> bool:
        """True when the loaded model's classes actually cover at least one PPE item.

        False for a person-only detector (COCO, `yolo26n`, pose-only): such a model cannot evidence
        a missing helmet, so `update()` raises no PPE violations at all while it is loaded.
        """

        return self._ppe_capable

    @property
    def class_labels(self) -> frozenset[str]:
        """The PPE vocabulary labels the loaded model can evidence (normalised)."""

        return self._class_labels

    @property
    def inferrable_items(self) -> tuple[str, ...]:
        """Items handled by the containment fallback: positive class present, `no_*` absent."""

        return self._inferrable

    # -- tracked items ---------------------------------------------------------

    @property
    def tracked_items(self) -> frozenset[str]:
        """The PPE items currently being enforced."""

        return self._tracked

    def set_tracked_items(
        self, items: Iterable[object] | Mapping[object, object] | None
    ) -> frozenset[str]:
        """Enable/disable individual PPE items and return the set that was applied.

        Accepts a mapping (`{"helmet": True, "goggles": False}`), any iterable of item names, or
        None for "all items". Unknown names are ignored (with a WARNING) rather than raising, so a
        bad REST payload can never take the safety loop down; compare the returned set against what
        you sent if you want to 400 on it.

        Thread-safety: the selection is a single immutable frozenset swapped in with one attribute
        assignment, so a concurrently-running `update()` sees either the old or the new set, never a
        half-applied one. No lock is needed or taken.
        """

        if items is None:
            applied = frozenset(TRACKED_ITEMS)
        elif isinstance(items, Mapping):
            requested = list(items.keys())
            enabled = [key for key, on in items.items() if on]
            applied = _coerce_items(enabled, source="set_tracked_items")
        elif isinstance(items, str):
            requested = [items]
            applied = _coerce_items([items], source="set_tracked_items")
        else:
            requested = list(items)
            applied = _coerce_items(requested, source="set_tracked_items")

        # Disabling every PPE check is a legitimate choice, but only an *explicit* one. If the
        # caller named items and none of them resolved, the payload was malformed — keep the
        # current selection rather than silently switching all PPE monitoring off.
        if items is not None and not applied and requested:
            recognised = _coerce_items(requested, source="set_tracked_items_probe")
            if not recognised:
                log.warning(
                    "refusing to disable all PPE checks: none of %r is a known item (%s). "
                    "Keeping the current selection: %s",
                    requested, ", ".join(TRACKED_ITEMS), ", ".join(sorted(self._tracked)) or "(none)",
                )
                return self._tracked

        self._tracked = applied
        log.info("PPE tracked items set to: %s", ", ".join(sorted(applied)) or "(none)")
        return applied

    # -- loading ---------------------------------------------------------------

    def _disable(self, model_path: str, cause: str) -> None:
        """Record + shout the reason PPE is off. Never raises."""

        self._model = None
        self.enabled = False
        self._reason = f"PPE model {model_path!r} {cause}. Remedy: {_REMEDY}. {_CONSEQUENCE}."
        log.warning(
            "PPE detector unavailable\n"
            "  tried path : %s\n"
            "  cause      : %s\n"
            "  remedy     : %s\n"
            "  consequence: %s.",
            model_path, cause, _REMEDY, _CONSEQUENCE,
        )

    def set_class_space(self, names: Mapping[int, object] | Iterable[object]) -> bool:
        """Declare the detector's label space and recompute the capability gate.

        Called by `_load`; also the seam a model-swap path (or a test) uses to describe a label
        space without loading weights. Returns `ppe_capable`.
        """

        if isinstance(names, Mapping):
            self._names = {int(k): str(v) for k, v in names.items()}
        else:
            self._names = {i: str(v) for i, v in enumerate(names)}
        raw = list(self._names.values())
        self._class_labels = canonical_labels(raw)
        self._ppe_capable = bool(self._class_labels & PPE_CLASS_NAMES)
        # A positive class with no matching `no_*` class is the only case where absence is evidence.
        self._inferrable = tuple(
            item
            for item in INFERRABLE_ITEMS
            if item in self._class_labels and MISSING_LABELS[item] not in self._class_labels
        )
        if raw and not self._ppe_capable:
            log.info(
                "PPE alerting paused: %s exposes no PPE classes (%s). A person-only detector "
                "cannot evidence a missing helmet, so no PPE violations will be raised. Remedy: %s",
                self._model_path or "the configured detector", ", ".join(sorted(raw)), _REMEDY,
            )
        return self._ppe_capable

    def _load(self, model_path: str, device: str) -> None:
        # A bare model name (e.g. "yolov8n.pt") is a valid ultralytics auto-download reference, so
        # only a path-shaped reference can be judged "missing" before we hand it to ultralytics.
        looks_like_path = os.sep in model_path or "/" in model_path
        if looks_like_path and not Path(model_path).is_file():
            self._disable(model_path, "does not exist (no such file)")
            return
        try:
            from ultralytics import YOLO

            model = YOLO(model_path)
            names = dict(model.names)
        except Exception as exc:  # noqa: BLE001 — PPE is optional; never take down the safety loop.
            self._disable(model_path, f"exists but failed to load ({type(exc).__name__}: {exc})")
            return

        self._model = model
        self._device = None if device == "auto" else device
        self.enabled = True
        self._reason = None
        log.info(
            "PPE detector loaded: %s (%d classes: %s)",
            model_path, len(names), ", ".join(sorted(str(v) for v in names.values())),
        )
        self.set_class_space(names)

    # -- rules -----------------------------------------------------------------

    def _match_person(self, bbox, persons: list[PersonDetection]) -> int | None:
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        for p in persons:
            x1, y1, x2, y2 = p.bbox
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                return p.track_id
        return None

    def _event(
        self,
        item: str,
        bbox: tuple[float, float, float, float],
        track_id: int | None,
        frame_index: int,
        now: float,
        meta: dict,
    ) -> HazardEvent | None:
        """Build a PPE_MISSING event, or None while the (track, item) cooldown is still running."""

        key = (track_id, item)
        if now - self._last_alert.get(key, -1e9) < self.cooldown_s:
            return None
        self._last_alert[key] = now
        who = f"Worker {track_id}" if track_id is not None else "A worker"
        return HazardEvent(
            hazard_type=HazardType.PPE_MISSING,
            severity=Severity.MEDIUM,
            message=f"{who} is missing {_ITEM_PHRASE[item]}.",
            frame_index=frame_index,
            ts_monotonic=now,
            track_id=track_id,
            bbox=bbox,
            meta=meta,
        )

    def evaluate(
        self,
        detections: Iterable[Mapping[str, object]],
        persons: list[PersonDetection],
        *,
        frame_index: int = 0,
        now: float = 0.0,
    ) -> list[HazardEvent]:
        """The PPE rules with no model and no frame involved — `update()` minus inference.

        `detections` is an iterable of ``{"label": <raw class name>, "bbox": (x1, y1, x2, y2),
        "conf": <float, optional>}``. Fills `last_boxes` / `equipment_boxes` and returns the hazard
        events the detections justify.
        """

        ppe_boxes: list[dict] = []
        equipment: list[dict] = []
        for det in detections:
            raw = str(det.get("label", ""))
            bbox = tuple(float(v) for v in det["bbox"])  # type: ignore[arg-type]
            item, ok = classify_label(raw)
            if item is not None:
                ppe_boxes.append(
                    {
                        "label": raw,                       # the detector's own spelling
                        "bbox": bbox,
                        "cat": item,                        # our vocabulary: helmet/vest/...
                        "ok": ok,
                        "norm": normalise_label(raw),
                        "conf": float(det.get("conf", 0.0) or 0.0),  # type: ignore[arg-type]
                        "tracked": item in self._tracked,   # False => drawn but never alerted on
                    }
                )
                continue
            kind = _equipment_kind(raw)
            if kind is not None:
                equipment.append({"label": raw, "bbox": bbox, "kind": kind})
        self.last_boxes = ppe_boxes
        self.equipment_boxes = equipment

        # The capability gate. Suppressing here (rather than at load) keeps the equipment boxes
        # from a person-only model available to proximity monitoring.
        if not self._ppe_capable or not persons:
            return []

        events: list[HazardEvent] = []

        # 1. explicit evidence: the model detected a `no_*` class.
        for box in ppe_boxes:
            if box["ok"] or not box["tracked"]:
                continue
            event = self._event(
                box["cat"],
                box["bbox"],
                self._match_person(box["bbox"], persons),
                frame_index,
                now,
                # `meta["class"]` carries the detector's RAW class name — the supervisor-feedback →
                # learning pipeline turns it into a training label, so it must name a real class.
                {"ppe": box["cat"], "class": box["label"], "inferred": False},
            )
            if event is not None:
                events.append(event)

        # 2. inferred evidence: positive-only label space, so "no vest inside this worker" is the
        #    only signal available. Never runs when a real `no_*` class exists (see _inferrable),
        #    so a violation is never double-reported.
        for item in self._inferrable:
            if item not in self._tracked:
                continue
            worn = [b["bbox"] for b in ppe_boxes if b["cat"] == item and b["ok"]]
            for person in persons:
                if any(center_inside(bbox, person.bbox) for bbox in worn):
                    continue
                event = self._event(
                    item,
                    person.bbox,
                    person.track_id,
                    frame_index,
                    now,
                    # No `no_*` class exists, so there is no raw class name to claim. `class` is
                    # left None on purpose: the feedback service then records no training label
                    # instead of inventing one the detector cannot predict.
                    {"ppe": item, "class": None, "inferred": True,
                     "basis": f"no {item} detected on this worker"},
                )
                if event is not None:
                    events.append(event)
        return events

    def _note_failure(self, stage: str, exc: Exception) -> None:
        """Report a contained failure once per kind, then stay quiet (this runs per frame)."""

        if stage in self._logged_failures:
            log.debug("PPE %s failed again: %s: %s", stage, type(exc).__name__, exc)
            return
        self._logged_failures.add(stage)
        log.warning(
            "PPE %s failed (%s: %s) — this frame is skipped; the safety loop keeps running",
            stage, type(exc).__name__, exc,
        )

    def _predict(self, image) -> list[dict]:
        preds = self._model.predict(image, conf=self.conf_min, device=self._device, verbose=False)
        out: list[dict] = []
        for r in preds:
            for b in r.boxes:
                try:
                    conf = float(b.conf[0])
                except Exception:  # noqa: BLE001 — conf is cosmetic; the box still matters.
                    conf = 0.0
                out.append(
                    {
                        "label": self._names.get(int(b.cls), ""),
                        "bbox": tuple(float(v) for v in b.xyxy[0].tolist()),
                        "conf": conf,
                    }
                )
        return out

    def update(self, result: FrameResult) -> list[HazardEvent]:
        self.last_boxes = []
        self.equipment_boxes = []
        if not self.enabled or self._model is None:
            return []
        try:
            detections = self._predict(result.frame.image)
        except Exception as exc:  # noqa: BLE001 — never take down the safety loop.
            self._note_failure("inference", exc)
            return []
        try:
            return self.evaluate(
                detections,
                result.persons,
                frame_index=result.frame.index,
                now=result.frame.ts_monotonic,
            )
        except Exception as exc:  # noqa: BLE001 — never take down the safety loop.
            self._note_failure("rule evaluation", exc)
            return []
