from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from fieldpilot.learning.captures import (
    CAPTURE_FRAMES_TABLE,
    CAPTURE_SESSIONS_TABLE,
    PPE_CLASSES,
    CaptureError,
    CaptureService,
    draft_boxes,
)
from fieldpilot.storage import DocStore


def _jpeg(width: int = 100, height: int = 80) -> bytes:
    image = np.full((height, width, 3), 127, dtype=np.uint8)
    ok, payload = cv2.imencode(".jpg", image)
    assert ok
    return payload.tobytes()


def test_draft_boxes_maps_classes_and_deduplicates_people() -> None:
    boxes = draft_boxes([
        {"class": "person", "confidence": 0.9, "box": [10, 10, 60, 70]},
        {"class": "Person", "confidence": 0.8, "box": [10, 10, 60, 70]},
        {"class": "no_hardhat", "confidence": 0.7, "box": [20, 10, 40, 30]},
        {"class": "crack", "confidence": 0.95, "box": [0, 0, 10, 10]},
    ], width=100, height=80)

    assert [box["label"] for box in boxes] == ["Person", "NO-Hardhat"]
    assert boxes[0]["xyxy"] == [0.1, 0.125, 0.6, 0.875]


@pytest.mark.asyncio
async def test_capture_review_and_session_safe_export(tmp_path: Path) -> None:
    docs = DocStore("sqlite", str(tmp_path / "platform.db"))
    await docs.start([CAPTURE_SESSIONS_TABLE, CAPTURE_FRAMES_TABLE])
    service = CaptureService(
        docs,
        capture_dir=str(tmp_path / "captures"),
        export_dir=str(tmp_path / "exports"),
    )
    try:
        train = await service.create_session(name="North gate morning", split="train")
        val = await service.create_session(name="South gate evening", split="val")
        train_frame = await service.capture_frame(
            session_id=train["session_id"],
            jpeg=_jpeg(),
            detections=[{"class": "helmet", "confidence": 0.92, "box": [10, 5, 50, 30]}],
            source_worker="w-1",
            zone="north",
            captured_at=100.0,
        )
        val_frame = await service.capture_frame(
            session_id=val["session_id"],
            jpeg=_jpeg(),
            detections=[],
            source_worker="w-2",
            zone="south",
            captured_at=200.0,
        )
        await service.update_frame(
            train_frame["frame_id"], boxes=train_frame["boxes"], review_status="reviewed"
        )
        await service.update_frame(val_frame["frame_id"], boxes=[], review_status="reviewed")

        exported = await service.export()
        data = yaml.safe_load(Path(exported["data_yaml"]).read_text(encoding="utf-8"))
        root = Path(data["path"])

        assert exported["images"] == {"train": 1, "val": 1, "test": 0}
        assert exported["boxes"] == {"Hardhat": 1}
        assert len(list((root / "images" / "train").glob("*.jpg"))) == 1
        assert len(list((root / "images" / "val").glob("*.jpg"))) == 1
        assert next((root / "labels" / "val").glob("*.txt")).read_text() == ""
        assert data["names"] == {index: name for index, name in enumerate(PPE_CLASSES)}
    finally:
        await docs.stop()


@pytest.mark.asyncio
async def test_export_requires_reviewed_train_and_val_sessions(tmp_path: Path) -> None:
    docs = DocStore("sqlite", str(tmp_path / "platform.db"))
    await docs.start([CAPTURE_SESSIONS_TABLE, CAPTURE_FRAMES_TABLE])
    service = CaptureService(
        docs,
        capture_dir=str(tmp_path / "captures"),
        export_dir=str(tmp_path / "exports"),
    )
    try:
        session = await service.create_session(name="Only training", split="train")
        frame = await service.capture_frame(
            session_id=session["session_id"],
            jpeg=_jpeg(),
            detections=[],
            source_worker="w-1",
            zone=None,
            captured_at=None,
        )
        await service.update_frame(frame["frame_id"], boxes=[], review_status="reviewed")

        with pytest.raises(CaptureError, match="reviewed frame in train and val"):
            await service.export()
    finally:
        await docs.stop()


@pytest.mark.asyncio
async def test_export_does_not_count_an_empty_val_session(tmp_path: Path) -> None:
    docs = DocStore("sqlite", str(tmp_path / "platform.db"))
    await docs.start([CAPTURE_SESSIONS_TABLE, CAPTURE_FRAMES_TABLE])
    service = CaptureService(
        docs,
        capture_dir=str(tmp_path / "captures"),
        export_dir=str(tmp_path / "exports"),
    )
    try:
        train = await service.create_session(name="Reviewed training", split="train")
        await service.create_session(name="Empty validation", split="val")
        frame = await service.capture_frame(
            session_id=train["session_id"],
            jpeg=_jpeg(),
            detections=[],
            source_worker="w-1",
            zone=None,
            captured_at=None,
        )
        await service.update_frame(frame["frame_id"], boxes=[], review_status="reviewed")

        with pytest.raises(CaptureError, match="reviewed frame in train and val"):
            await service.export()
    finally:
        await docs.stop()
