"""PPE label handling: any public checkpoint works, and a person-only model never invents alerts.

Hermetic by construction — `detection.ppe_model` is null in every checker built here, so no weights,
no ultralytics and no camera are involved. The label space is declared with `set_class_space()` and
the detections are handed to `evaluate()` directly, which is exactly the split `update()` uses
(inference, then rules).

The point being defended: PPE datasets spell the same class a dozen ways, and the wrong reaction to
an unrecognised label is to alert anyway. A model that has no PPE classes at all cannot evidence a
missing helmet, so it must raise nothing.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from fieldpilot.core.types import HazardType, Severity
from fieldpilot.safety.ppe import (
    MISSING_LABELS,
    PPE_CLASS_NAMES,
    TRACKED_ITEMS,
    PPEChecker,
    canonical_labels,
    center_inside,
    classify_label,
    normalise_label,
)
from tests.conftest import make_cfg, make_person, make_result

# A full construction-PPE label space (positives + explicit negatives), as shipped by the
# 10-class CSS/construction checkpoints.
FULL_SPACE = {
    0: "Hardhat",
    1: "NO-Hardhat",
    2: "Safety Vest",
    3: "NO-Safety Vest",
    4: "Gloves",
    5: "NO-Gloves",
    6: "safety_shoes",
    7: "no_safety_shoes",
    8: "Goggles",
    9: "no-goggle",
    10: "Person",
    11: "machinery",
}
PERSON_ONLY_SPACE = {0: "person", 1: "car"}


def make_checker(**sections) -> PPEChecker:
    """A checker that loads nothing: `detection.ppe_model` is null, so no weights are touched."""

    sections.setdefault("detection", {})["ppe_model"] = None
    checker = PPEChecker(make_cfg(**sections))
    assert checker.enabled is False and checker.ppe_capable is False
    return checker


def det(label: str, bbox: tuple[float, float, float, float], conf: float = 0.9) -> dict:
    return {"label": label, "bbox": bbox, "conf": conf}


# `make_person(1, 120, 220)` -> bbox (280, 90, 360, 360); `make_person(2, ..., x=100)` -> (60, ...).
HEAD_INSIDE_1 = (300.0, 95.0, 340.0, 125.0)
TORSO_INSIDE_1 = (295.0, 130.0, 345.0, 210.0)
FAR_AWAY = (600.0, 400.0, 630.0, 440.0)


def person(track_id: int, x: float = 320) -> object:
    return make_person(track_id, shoulder_y=120, hip_y=220, shoulder_x=x, hip_x=x)


# ------------------------------------------------------------------ alias normalisation

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # the three spellings of the same missing-helmet class, from three different datasets
        ("NO-Hardhat", "no_helmet"),
        ("no hardhat", "no_helmet"),
        ("no_hardhat", "no_helmet"),
        ("NOHardhat", "no_helmet"),
        ("NO-Hat", "no_helmet"),
        ("without_helmet", "no_helmet"),
        # helmet positives
        ("Hardhat", "helmet"),
        ("hard hat", "helmet"),
        ("HARD-HAT", "helmet"),
        ("safety helmet", "helmet"),
        ("  Helmet  ", "helmet"),
        # vest
        ("Safety Vest", "vest"),
        ("high-visibility vest", "vest"),
        ("NO-Safety Vest", "no_vest"),
        ("no_safety_vest", "no_vest"),
        # gloves / boots / goggles
        ("Glove", "gloves"),
        ("no-glove", "no_gloves"),
        ("safety_shoes", "boots"),
        ("Safety Shoe", "boots"),
        ("Boot", "boots"),
        ("no_safety_shoes", "no_boots"),
        ("Safety Glasses", "goggles"),
        ("goggle", "goggles"),
        ("no-goggle", "no_goggles"),
        ("no goggles", "no_goggles"),
        # people, and labels that are none of our business
        ("Worker", "person"),
        ("Person", "person"),
        ("machinery", "machinery"),
        ("Safety Cone", "safety_cone"),
    ],
)
def test_normalise_label_is_punctuation_case_and_separator_insensitive(raw, expected):
    assert normalise_label(raw) == expected


def test_the_three_no_hardhat_spellings_are_the_same_class():
    forms = {normalise_label(x) for x in ("NO-Hardhat", "no hardhat", "no_hardhat", "NO_HARDHAT")}
    assert forms == {"no_helmet"}


@pytest.mark.parametrize(
    ("raw", "item", "compliant"),
    [
        ("Hardhat", "helmet", True),
        ("NO-Hardhat", "helmet", False),
        ("Safety Vest", "vest", True),
        ("NO-Safety Vest", "vest", False),
        ("Gloves", "gloves", True),
        ("no_gloves", "gloves", False),
        ("safety_shoes", "boots", True),
        ("NO-Safety Shoes", "boots", False),
        ("Goggles", "goggles", True),
        ("no-goggle", "goggles", False),
        # uncatalogued spellings still land on the right item via the keyword fallback
        ("Vest-Worn", "vest", True),
        ("Reflective-Vest", "vest", True),
        # not PPE
        ("Person", None, True),
        ("machinery", None, True),
        ("Safety Cone", None, True),
        ("hatch", None, True),  # "hat" only matches as a whole token
    ],
)
def test_classify_label(raw, item, compliant):
    assert classify_label(raw) == (item, compliant)


def test_the_vocabulary_covers_five_items_with_both_polarities():
    assert TRACKED_ITEMS == ("helmet", "vest", "gloves", "boots", "goggles")
    assert set(MISSING_LABELS) == set(TRACKED_ITEMS)
    assert PPE_CLASS_NAMES == set(TRACKED_ITEMS) | set(MISSING_LABELS.values())
    assert len(PPE_CLASS_NAMES) == 10


def test_canonical_labels_reports_what_a_model_can_evidence():
    assert canonical_labels(FULL_SPACE.values()) == PPE_CLASS_NAMES
    assert canonical_labels(PERSON_ONLY_SPACE.values()) == frozenset()
    assert canonical_labels(["person", "vest"]) == frozenset({"vest"})


def test_center_inside():
    assert center_inside((300, 95, 340, 125), (280, 90, 360, 360)) is True
    assert center_inside(FAR_AWAY, (280, 90, 360, 360)) is False


# ------------------------------------------------------------------ the capability gate

def test_a_person_only_model_is_not_ppe_capable_and_says_so_once_at_info(caplog):
    checker = make_checker()
    with caplog.at_level(logging.INFO, logger="fieldpilot.safety.ppe"):
        assert checker.set_class_space(PERSON_ONLY_SPACE) is False

    assert checker.ppe_capable is False
    assert checker.inferrable_items == ()
    paused = [r for r in caplog.records if "alerting paused" in r.getMessage()]
    assert len(paused) == 1, "the reason PPE is quiet must be stated exactly once"
    assert paused[0].levelno == logging.INFO, "a person-only model is a choice, not a fault"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_real_ppe_model_is_capable_and_stays_quiet(caplog):
    checker = make_checker()
    with caplog.at_level(logging.INFO, logger="fieldpilot.safety.ppe"):
        assert checker.set_class_space(FULL_SPACE) is True

    assert checker.ppe_capable is True
    assert checker.class_labels == PPE_CLASS_NAMES
    assert checker.inferrable_items == ()  # every negative class is present, so nothing is inferred
    assert not [r for r in caplog.records if "alerting paused" in r.getMessage()]


def test_a_person_only_model_raises_no_ppe_violations_at_all():
    """The important one: a model with no PPE classes cannot evidence a missing helmet."""

    checker = make_checker()
    checker.set_class_space(PERSON_ONLY_SPACE)
    persons = [person(1)]

    events = checker.evaluate(
        [det("person", (280, 90, 360, 360)), det("NO-Hardhat", HEAD_INSIDE_1)],
        persons,
        now=100.0,
    )
    assert events == [], "PPE alerts from a person-only class space would be fabricated evidence"


def test_a_person_only_model_still_feeds_proximity_with_equipment():
    checker = make_checker()
    checker.set_class_space({0: "person", 1: "truck"})
    checker.evaluate([det("truck", (10, 10, 120, 90))], [person(1)], now=100.0)
    assert checker.equipment_boxes == [
        {"label": "truck", "bbox": (10.0, 10.0, 120.0, 90.0), "kind": "vehicle"}
    ]


def test_capability_is_reported_in_the_status_view():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)

    # the published 3-key contract other modules already depend on is unchanged
    assert set(checker.status) == {"enabled", "model", "reason"}
    assert set(checker.describe()) == {"enabled", "model", "reason"}

    full = checker.describe(full=True)
    assert full["ppe_capable"] is True
    assert full["tracked_items"] == sorted(TRACKED_ITEMS)
    assert sorted(full["class_labels"]) == sorted(PPE_CLASS_NAMES)
    assert full["inferrable_items"] == []

    full["ppe_capable"] = "tampered"  # a health endpoint must not be able to change our state
    assert checker.ppe_capable is True


# ------------------------------------------------------------------ explicit violations

def test_an_explicit_negative_class_raises_an_event_carrying_the_raw_class_name():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    persons = [person(1)]

    events = checker.evaluate([det("NO-Hardhat", HEAD_INSIDE_1)], persons, frame_index=7, now=100.0)

    assert len(events) == 1
    event = events[0]
    assert event.hazard_type is HazardType.PPE_MISSING
    assert event.severity is Severity.MEDIUM
    assert event.track_id == 1 and event.frame_index == 7
    assert event.message == "Worker 1 is missing a hard hat."
    # the learning pipeline builds training labels from meta["class"], so it must be the detector's
    # own spelling, not our normalised vocabulary
    assert event.meta["class"] == "NO-Hardhat"
    assert event.meta["ppe"] == "helmet" and event.meta["inferred"] is False


def test_all_five_items_can_raise_a_violation():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    persons = [person(1)]
    labels = ["NO-Hardhat", "NO-Safety Vest", "NO-Gloves", "no_safety_shoes", "no-goggle"]

    events = checker.evaluate([det(x, TORSO_INSIDE_1) for x in labels], persons, now=100.0)
    assert {e.meta["ppe"] for e in events} == set(TRACKED_ITEMS)
    assert {e.meta["class"] for e in events} == set(labels)


def test_last_boxes_keep_their_overlay_shape():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    checker.evaluate(
        [det("Hardhat", HEAD_INSIDE_1), det("NO-Safety Vest", TORSO_INSIDE_1),
         det("machinery", (10, 10, 60, 60))],
        [person(1)],
        now=100.0,
    )

    assert [b["label"] for b in checker.last_boxes] == ["Hardhat", "NO-Safety Vest"]
    for box in checker.last_boxes:
        assert {"label", "bbox", "cat", "ok"} <= set(box)
        assert isinstance(box["bbox"], tuple) and len(box["bbox"]) == 4
    assert checker.last_boxes[0]["cat"] == "helmet" and checker.last_boxes[0]["ok"] is True
    assert checker.last_boxes[1]["cat"] == "vest" and checker.last_boxes[1]["ok"] is False
    assert checker.equipment_boxes[0]["kind"] == "machinery"


def test_a_compliant_worker_raises_nothing():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    events = checker.evaluate(
        [det("Hardhat", HEAD_INSIDE_1), det("Safety Vest", TORSO_INSIDE_1)],
        [person(1)],
        now=100.0,
    )
    assert events == []


# ------------------------------------------------------------------ positive-only fallback

POSITIVE_ONLY_VEST = {0: "person", 1: "vest"}


def test_vest_without_a_no_vest_class_falls_back_to_containment():
    checker = make_checker()
    checker.set_class_space(POSITIVE_ONLY_VEST)
    assert checker.inferrable_items == ("vest",)

    events = checker.evaluate([], [person(1)], now=100.0)

    assert len(events) == 1
    event = events[0]
    assert event.meta["ppe"] == "vest" and event.meta["inferred"] is True
    assert event.bbox == person(1).bbox
    assert event.track_id == 1
    # there is no `no_vest` class to name, so no training label is claimed
    assert event.meta["class"] is None


def test_the_fallback_does_not_fire_when_a_vest_centre_is_inside_the_worker():
    checker = make_checker()
    checker.set_class_space(POSITIVE_ONLY_VEST)
    assert checker.evaluate([det("vest", TORSO_INSIDE_1)], [person(1)], now=100.0) == []


def test_the_fallback_ignores_a_vest_belonging_to_someone_else():
    checker = make_checker()
    checker.set_class_space(POSITIVE_ONLY_VEST)
    # the vest sits on worker 2, so worker 1 is still uncovered
    events = checker.evaluate([det("vest", (80, 130, 120, 210))], [person(1), person(2, x=100)],
                              now=100.0)
    assert [e.track_id for e in events] == [1]


def test_the_fallback_does_not_run_when_a_real_no_vest_class_exists():
    """Never double-report: with an explicit negative class, absence proves nothing."""

    checker = make_checker()
    checker.set_class_space({0: "person", 1: "Safety Vest", 2: "NO-Safety Vest"})
    assert checker.inferrable_items == ()
    assert checker.evaluate([], [person(1)], now=100.0) == []

    events = checker.evaluate([det("NO-Safety Vest", TORSO_INSIDE_1)], [person(1)], now=100.0)
    assert [e.meta["class"] for e in events] == ["NO-Safety Vest"]


def test_a_positive_only_helmet_space_is_inferred_too():
    checker = make_checker()
    checker.set_class_space({0: "person", 1: "Hardhat"})
    assert checker.inferrable_items == ("helmet",)
    assert [e.meta["ppe"] for e in checker.evaluate([], [person(1)], now=100.0)] == ["helmet"]
    assert checker.evaluate([det("Hardhat", HEAD_INSIDE_1)], [person(1)], now=200.0) == []


def test_small_items_are_never_inferred_from_absence():
    """Gloves/boots/goggles are too small and too occluded for "not detected" to mean "not worn"."""

    checker = make_checker()
    checker.set_class_space({0: "person", 1: "Gloves", 2: "Goggles", 3: "safety_shoes"})
    assert checker.ppe_capable is True
    assert checker.inferrable_items == ()
    assert checker.evaluate([], [person(1)], now=100.0) == []


def test_no_persons_means_no_inferred_violations():
    checker = make_checker()
    checker.set_class_space(POSITIVE_ONLY_VEST)
    assert checker.evaluate([det("vest", TORSO_INSIDE_1)], [], now=100.0) == []


# ------------------------------------------------------------------ per-item enable/disable

def test_tracked_items_default_to_everything_when_config_is_silent():
    assert make_checker().tracked_items == set(TRACKED_ITEMS)


def test_tracked_items_boot_default_comes_from_config():
    checker = make_checker(
        safety={"tracked_items": {"helmet": True, "vest": True, "gloves": False,
                                  "boots": False, "goggles": False}}
    )
    assert checker.tracked_items == {"helmet", "vest"}


def test_a_config_list_of_items_also_works():
    assert make_checker(safety={"tracked_items": ["Helmet", "goggles"]}).tracked_items == {
        "helmet", "goggles"
    }


def test_disabling_an_item_suppresses_only_that_item():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    assert checker.set_tracked_items({"helmet": True, "vest": False}) == {"helmet"}

    events = checker.evaluate(
        [det("NO-Hardhat", HEAD_INSIDE_1), det("NO-Safety Vest", TORSO_INSIDE_1)],
        [person(1)],
        now=100.0,
    )
    assert [e.meta["ppe"] for e in events] == ["helmet"]
    # the untracked detection is still drawn, flagged as out of scope rather than hidden
    assert [(b["cat"], b["tracked"]) for b in checker.last_boxes] == [
        ("helmet", True), ("vest", False)
    ]


def test_disabling_an_item_also_stops_its_inferred_violations():
    checker = make_checker()
    checker.set_class_space(POSITIVE_ONLY_VEST)
    checker.set_tracked_items(["helmet"])
    assert checker.evaluate([], [person(1)], now=100.0) == []


def test_set_tracked_items_accepts_negative_names_and_drops_unknown_ones(caplog):
    checker = make_checker()
    with caplog.at_level(logging.WARNING, logger="fieldpilot.safety.ppe"):
        applied = checker.set_tracked_items(["NO-Hardhat", "sunglasses"])
    assert applied == {"helmet"}
    assert any("sunglasses" in r.getMessage() for r in caplog.records)

    assert checker.set_tracked_items(None) == set(TRACKED_ITEMS)
    assert checker.set_tracked_items([]) == frozenset()


def test_tracked_items_cannot_be_mutated_through_the_property():
    checker = make_checker()
    with pytest.raises(AttributeError):
        checker.tracked_items = {"helmet"}  # type: ignore[misc]


# ------------------------------------------------------------------ cooldowns

def test_cooldowns_are_independent_per_track_and_item():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    checker.cooldown_s = 20.0
    p1, p2 = person(1), person(2, x=100)

    first = checker.evaluate([det("NO-Hardhat", HEAD_INSIDE_1)], [p1, p2], now=100.0)
    assert [(e.track_id, e.meta["ppe"]) for e in first] == [(1, "helmet")]

    # same worker, same item, inside the cooldown -> suppressed; a *different* item on the same
    # worker must still get through
    second = checker.evaluate(
        [det("NO-Hardhat", HEAD_INSIDE_1), det("NO-Gloves", TORSO_INSIDE_1)],
        [p1, p2],
        now=101.0,
    )
    assert [(e.track_id, e.meta["ppe"]) for e in second] == [(1, "gloves")]

    # a different worker with the same item is a different alert
    third = checker.evaluate([det("NO-Hardhat", (80, 95, 120, 125))], [p1, p2], now=102.0)
    assert [(e.track_id, e.meta["ppe"]) for e in third] == [(2, "helmet")]

    # and the original pair repeats once the cooldown has elapsed
    fourth = checker.evaluate([det("NO-Hardhat", HEAD_INSIDE_1)], [p1, p2], now=125.0)
    assert [(e.track_id, e.meta["ppe"]) for e in fourth] == [(1, "helmet")]


def test_inferred_violations_share_the_same_cooldown_bookkeeping():
    checker = make_checker()
    checker.set_class_space(POSITIVE_ONLY_VEST)
    checker.cooldown_s = 20.0
    assert len(checker.evaluate([], [person(1)], now=100.0)) == 1
    assert checker.evaluate([], [person(1)], now=101.0) == []
    assert len(checker.evaluate([], [person(1)], now=130.0)) == 1


# ------------------------------------------------------------------ update() plumbing / containment

class _FakeBox:
    def __init__(self, cls: int, xyxy, conf: float = 0.9):
        self.cls = cls
        self.xyxy = np.array([xyxy], dtype=float)
        self.conf = np.array([conf], dtype=float)


class _FakeResult:
    def __init__(self, boxes):
        self.boxes = boxes


class _FakeModel:
    """Just enough of the ultralytics surface `_predict` touches."""

    def __init__(self, boxes):
        self._boxes = boxes

    def predict(self, image, **kwargs):
        return [_FakeResult(self._boxes)]


class _BrokenModel:
    def predict(self, image, **kwargs):
        raise RuntimeError("CUDA device disappeared")


def _armed(checker: PPEChecker, model) -> PPEChecker:
    """Arm a weightless checker with a fake detector (what `_load` would otherwise do)."""

    checker._model = model
    checker.enabled = True
    return checker


def test_update_maps_class_ids_through_the_label_space():
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    _armed(checker, _FakeModel([_FakeBox(1, HEAD_INSIDE_1), _FakeBox(11, (10, 10, 60, 60))]))

    result = make_result(100.0, 3, [person(1)])
    events = checker.update(result)

    assert [e.meta["class"] for e in events] == ["NO-Hardhat"]
    assert [b["label"] for b in checker.last_boxes] == ["NO-Hardhat"]
    assert checker.last_boxes[0]["conf"] == pytest.approx(0.9)
    assert [b["kind"] for b in checker.equipment_boxes] == ["machinery"]


def test_update_on_a_person_only_model_returns_nothing_even_with_boxes():
    checker = make_checker()
    checker.set_class_space(PERSON_ONLY_SPACE)
    _armed(checker, _FakeModel([_FakeBox(0, (280, 90, 360, 360))]))
    assert checker.update(make_result(100.0, 1, [person(1)])) == []


def test_a_failing_detector_never_takes_down_the_safety_loop(caplog):
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    _armed(checker, _BrokenModel())

    with caplog.at_level(logging.WARNING, logger="fieldpilot.safety.ppe"):
        assert checker.update(make_result(100.0, 1, [person(1)])) == []
        assert checker.update(make_result(100.1, 2, [person(1)])) == []

    assert checker.last_boxes == [] and checker.equipment_boxes == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "the failure is stated once, not once per frame"
    assert "CUDA device disappeared" in warnings[0].getMessage()


def test_a_malformed_detection_is_contained_too(caplog):
    checker = make_checker()
    checker.set_class_space(FULL_SPACE)
    _armed(checker, _FakeModel([_FakeBox(1, HEAD_INSIDE_1)]))
    checker._names = {}  # a label space that disagrees with the model's ids

    # a class id absent from the label space degrades to an unknown (non-PPE) label, not a crash
    assert checker.update(make_result(100.0, 1, [person(1)])) == []
    assert checker.last_boxes == []


# ----------------------------------------------- malformed tracked-items payloads


def test_a_malformed_tracked_items_payload_does_not_disable_every_check():
    """Turning off all PPE monitoring must require an explicit request, never a bad payload.

    A settings message whose item names are all unrecognised previously resolved to the empty set
    and silently switched every PPE check off — the most dangerous possible reading of "I could
    not understand you".
    """

    checker = make_checker()
    before = checker.tracked_items
    assert before, "a fresh checker should start with items enabled"

    assert checker.set_tracked_items("nonsense") == before
    assert checker.tracked_items == before

    assert checker.set_tracked_items(["jetpack", "parachute"]) == before
    assert checker.tracked_items == before


def test_an_explicit_all_off_is_still_honoured():
    checker = make_checker()
    applied = checker.set_tracked_items({item: False for item in TRACKED_ITEMS})
    assert applied == frozenset()
    assert checker.tracked_items == frozenset()

    checker.set_tracked_items(None)
    assert checker.tracked_items == frozenset(TRACKED_ITEMS)
    assert checker.set_tracked_items([]) == frozenset(), "an empty collection is a real choice"


def test_a_partially_valid_payload_applies_the_valid_part():
    checker = make_checker()
    applied = checker.set_tracked_items(["helmet", "jetpack"])
    assert applied == frozenset({"helmet"}), "a recognised name means the payload was understood"
