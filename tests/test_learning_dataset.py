"""Feedback rows -> YOLO dataset. Builder only: no ultralytics, no training, no weights."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from fieldpilot.learning.dataset import build_dataset

CLASSES = {0: "Hardhat", 1: "NO-Hardhat", 2: "Person"}


def write_jpeg(path, *, w: int = 200, h: int = 100) -> str:
    """A real JPEG on disk so `cv2.imread` returns a frame with known dimensions."""

    img = np.full((h, w, 3), 128, dtype=np.uint8)
    assert cv2.imwrite(str(path), img), f"cv2 failed to write {path}"
    return str(path)


@pytest.fixture
def frame(tmp_path):
    return write_jpeg(tmp_path / "frame_a.jpg")


@pytest.fixture
def out(tmp_path):
    return tmp_path / "ds"


def labels_of(out, stem: str) -> str:
    return (out / "labels" / "train" / f"{stem}.txt").read_text()


def label_files(out) -> list[str]:
    return sorted(p.name for p in (out / "labels" / "train").iterdir())


def image_files(out) -> list[str]:
    return sorted(p.name for p in (out / "images" / "train").iterdir())


# --------------------------------------------------------------------------- positives


def test_approved_row_becomes_a_normalised_positive_label(frame, out):
    rows = [{"decision": "approve", "label": "NO-Hardhat", "image_path": frame,
             "bbox": [20, 10, 60, 50]}]
    summary = build_dataset(rows, out, class_names=CLASSES)

    assert summary.positives == 1
    assert summary.negatives == 0
    assert summary.skipped == 0
    assert summary.skip_reasons == {}
    assert summary.usable == 1
    assert summary.class_names == CLASSES

    # 200x100 image: cx=40/200, cy=30/100, w=40/200, h=40/100
    assert labels_of(out, "000000_frame_a") == "1 0.200000 0.300000 0.200000 0.400000\n"
    assert image_files(out) == ["000000_frame_a.jpg"]


def test_bbox_is_clamped_to_the_image(frame, out):
    rows = [{"decision": "approve", "label": "Hardhat", "image_path": frame,
             "bbox": [-50, -50, 250, 150]}]
    summary = build_dataset(rows, out, class_names=CLASSES)
    assert summary.positives == 1
    assert labels_of(out, "000000_frame_a") == "0 0.500000 0.500000 1.000000 1.000000\n"


def test_bbox_corners_are_normalised_regardless_of_order(frame, out):
    forward = build_dataset(
        [{"decision": "approve", "label": "Person", "image_path": frame,
          "bbox": [20, 10, 60, 50]}],
        out, class_names=CLASSES,
    )
    forward_label = labels_of(out, "000000_frame_a")
    reversed_ = build_dataset(
        [{"decision": "approve", "label": "Person", "image_path": frame,
          "bbox": [60, 50, 20, 10]}],
        out, class_names=CLASSES,
    )
    assert forward.positives == reversed_.positives == 1
    assert labels_of(out, "000000_frame_a") == forward_label
    assert forward_label == "2 0.200000 0.300000 0.200000 0.400000\n"


def test_bbox_accepts_a_tuple(frame, out):
    summary = build_dataset(
        [{"decision": "approve", "label": "Hardhat", "image_path": frame,
          "bbox": (20, 10, 60, 50)}],
        out, class_names=CLASSES,
    )
    assert summary.positives == 1


def test_class_matching_is_case_and_separator_insensitive(tmp_path, out):
    frames = [write_jpeg(tmp_path / f"f{i}.jpg") for i in range(4)]
    variants = ["no-hardhat", "NO_HARDHAT", "no hardhat", "NO-Hardhat"]
    rows = [
        {"decision": "approve", "label": label, "image_path": path, "bbox": [20, 10, 60, 50]}
        for label, path in zip(variants, frames, strict=True)
    ]
    summary = build_dataset(rows, out, class_names=CLASSES)

    assert summary.positives == 4
    assert summary.skipped == 0
    written = [labels_of(out, f"{i:06d}_f{i}") for i in range(4)]
    assert all(line.startswith("1 ") for line in written), written


def test_exact_class_name_beats_the_containment_fallback(frame, out):
    # "Hardhat" is a substring of "NO-Hardhat"; an exact match must still win
    summary = build_dataset(
        [{"decision": "approve", "label": "Hardhat", "image_path": frame,
          "bbox": [20, 10, 60, 50]}],
        out, class_names=CLASSES,
    )
    assert summary.positives == 1
    assert labels_of(out, "000000_frame_a").startswith("0 ")


# --------------------------------------------------------------------------- negatives


def test_rejected_row_becomes_an_empty_background_label(frame, out):
    summary = build_dataset(
        [{"decision": "reject", "label": "NO-Hardhat", "image_path": frame}],
        out, class_names=CLASSES,
    )
    assert summary.negatives == 1
    assert summary.positives == 0
    assert summary.skipped == 0
    assert summary.usable == 1
    assert labels_of(out, "000000_frame_a") == ""
    assert image_files(out) == ["000000_frame_a.jpg"]


def test_rejected_row_needs_no_bbox_or_known_label(frame, out):
    summary = build_dataset(
        [{"decision": "reject", "label": "banana", "image_path": frame}],
        out, class_names=CLASSES,
    )
    assert summary.negatives == 1
    assert summary.skipped == 0


def test_decision_case_is_normalised(frame, out):
    summary = build_dataset(
        [{"decision": "APPROVE", "label": "Person", "image_path": frame,
          "bbox": [20, 10, 60, 50]},
         {"decision": "Reject", "label": "Person", "image_path": write_jpeg(
             frame.replace("frame_a", "frame_b"))}],
        out, class_names=CLASSES,
    )
    assert (summary.positives, summary.negatives, summary.skipped) == (1, 1, 0)


# --------------------------------------------------------------------------- skips


def test_every_skip_reason_is_recorded(tmp_path, frame, out):
    rows = [
        # no frame on disk
        {"decision": "approve", "label": "Hardhat", "image_path": str(tmp_path / "gone.jpg"),
         "bbox": [20, 10, 60, 50]},
        # no image_path at all
        {"decision": "approve", "label": "Hardhat", "bbox": [20, 10, 60, 50]},
        # approved but the reviewer gave no box
        {"decision": "approve", "label": "Hardhat", "image_path": frame},
        # zero-width box
        {"decision": "approve", "label": "Hardhat", "image_path": frame, "bbox": [10, 10, 10, 50]},
        # label the base model cannot represent
        {"decision": "approve", "label": "banana", "image_path": frame,
         "bbox": [20, 10, 60, 50]},
        # neither approve nor reject
        {"decision": "pending", "label": "Hardhat", "image_path": frame},
    ]
    summary = build_dataset(rows, out, class_names=CLASSES)

    assert summary.skipped == 6
    assert summary.skip_reasons == {
        "missing_image": 2,
        "missing_bbox": 1,
        "degenerate_bbox": 1,
        "label_outside_model_classes": 1,
        "unknown_decision": 1,
    }
    assert summary.positives == 0
    assert summary.negatives == 0
    assert summary.usable == 0
    assert label_files(out) == []
    assert image_files(out) == []


def test_short_bbox_is_treated_as_missing(frame, out):
    summary = build_dataset(
        [{"decision": "approve", "label": "Hardhat", "image_path": frame, "bbox": [20, 10, 60]}],
        out, class_names=CLASSES,
    )
    assert summary.skip_reasons == {"missing_bbox": 1}


def test_sub_pixel_box_is_degenerate(frame, out):
    # 0.05px wide on a 200px image is below the 0.001 normalised floor
    summary = build_dataset(
        [{"decision": "approve", "label": "Hardhat", "image_path": frame,
          "bbox": [10, 10, 10.05, 50]}],
        out, class_names=CLASSES,
    )
    assert summary.skip_reasons == {"degenerate_bbox": 1}


def test_skips_do_not_disturb_the_usable_rows(tmp_path, out):
    good = write_jpeg(tmp_path / "good.jpg")
    rows = [
        {"decision": "approve", "label": "banana", "image_path": good, "bbox": [20, 10, 60, 50]},
        {"decision": "approve", "label": "Person", "image_path": good, "bbox": [20, 10, 60, 50]},
    ]
    summary = build_dataset(rows, out, class_names=CLASSES)
    assert summary.positives == 1
    assert summary.skipped == 1
    # the surviving row keeps its own row index in the stem
    assert label_files(out) == ["000001_good.txt"]
    assert labels_of(out, "000001_good").startswith("2 ")


def test_empty_row_list_produces_an_empty_dataset(out):
    summary = build_dataset([], out, class_names=CLASSES)
    assert summary.to_dict()["usable"] == 0
    assert label_files(out) == []
    assert (out / "data.yaml").is_file()


# --------------------------------------------------------------------------- data.yaml


def test_data_yaml_validates_against_the_locked_val_set(tmp_path, frame, out):
    val = tmp_path / "locked" / "val" / "images"
    val.mkdir(parents=True)
    summary = build_dataset(
        [{"decision": "reject", "label": "Person", "image_path": frame}],
        out, class_names=CLASSES, val_images_dir=val,
    )
    text = (out / "data.yaml").read_text()

    assert f"path: {out.resolve()}" in text
    assert "train: images/train" in text
    assert f"val: {val.resolve()}" in text
    assert "names:\n  0: Hardhat\n  1: NO-Hardhat\n  2: Person\n" in text
    assert summary.data_yaml == str(out / "data.yaml")
    assert summary.out_dir == str(out)


def test_data_yaml_falls_back_to_the_train_split_without_a_locked_val_dir(frame, out):
    build_dataset(
        [{"decision": "reject", "label": "Person", "image_path": frame}],
        out, class_names=CLASSES,
    )
    text = (out / "data.yaml").read_text()
    assert "val: images/train" in text


def test_class_names_are_written_in_index_order(frame, out):
    build_dataset([], out, class_names={2: "Person", 0: "Hardhat", 1: "NO-Hardhat"})
    text = (out / "data.yaml").read_text()
    assert text.index("0: Hardhat") < text.index("1: NO-Hardhat") < text.index("2: Person")


# --------------------------------------------------------------------------- rebuilds


def test_rebuild_wipes_the_previous_dataset(tmp_path, out):
    a = write_jpeg(tmp_path / "a.jpg")
    b = write_jpeg(tmp_path / "b.jpg")
    build_dataset(
        [{"decision": "reject", "image_path": a}, {"decision": "reject", "image_path": b}],
        out, class_names=CLASSES,
    )
    assert len(image_files(out)) == 2

    summary = build_dataset([{"decision": "reject", "image_path": a}], out, class_names=CLASSES)
    assert summary.negatives == 1
    assert image_files(out) == ["000000_a.jpg"]
    assert label_files(out) == ["000000_a.txt"]


def test_summary_to_dict_is_json_friendly(frame, out):
    summary = build_dataset(
        [{"decision": "approve", "label": "Person", "image_path": frame,
          "bbox": [20, 10, 60, 50]}],
        out, class_names=CLASSES,
    )
    d = summary.to_dict()
    assert d["positives"] == 1
    assert d["negatives"] == 0
    assert d["skipped"] == 0
    assert d["usable"] == 1
    assert d["skip_reasons"] == {}
    assert d["class_names"] == CLASSES
    assert d["out_dir"] == str(out)
