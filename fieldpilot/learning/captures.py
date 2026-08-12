"""Manager-reviewed phone-frame capture and leakage-safe YOLO export."""

from __future__ import annotations

import re
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from fieldpilot.storage import Column, DocStore, TableSpec

PPE_CLASSES = (
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
)
CAPTURE_SPLITS = ("train", "val", "test")

CAPTURE_SESSIONS_TABLE = TableSpec(
    "capture_sessions",
    key="session_id",
    columns=(
        Column("name"),
        Column("split", indexed=True),
        Column("created_at", "real"),
        Column("updated_at", "real"),
    ),
)
CAPTURE_FRAMES_TABLE = TableSpec(
    "capture_frames",
    key="frame_id",
    columns=(
        Column("session_id", indexed=True),
        Column("review_status", indexed=True),
        Column("captured_at", "real"),
        Column("source_worker"),
        Column("zone"),
        Column("created_at", "real"),
        Column("updated_at", "real"),
    ),
)


class CaptureError(ValueError):
    """A capture request cannot be stored or exported safely."""


def _class_id(label: object) -> int | None:
    key = re.sub(r"[^a-z0-9]+", "", str(label).strip().lower())
    aliases = {
        "helmet": "hardhat",
        "nohelmet": "nohardhat",
        "vest": "safetyvest",
        "novest": "nosafetyvest",
        "cone": "safetycone",
    }
    key = aliases.get(key, key)
    for index, name in enumerate(PPE_CLASSES):
        if re.sub(r"[^a-z0-9]+", "", name.lower()) == key:
            return index
    return None


def _clamp(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CaptureError("box coordinates must be numbers") from exc
    return min(1.0, max(0.0, number))


def _validate_boxes(boxes: object) -> list[dict[str, Any]]:
    if not isinstance(boxes, list):
        raise CaptureError("boxes must be a list")
    validated: list[dict[str, Any]] = []
    for item in boxes:
        if not isinstance(item, dict):
            raise CaptureError("each box must be an object")
        class_id = item.get("class_id")
        if not isinstance(class_id, int) or not 0 <= class_id < len(PPE_CLASSES):
            raise CaptureError(f"box class_id must be between 0 and {len(PPE_CLASSES) - 1}")
        xyxy = item.get("xyxy")
        if not isinstance(xyxy, list) or len(xyxy) != 4:
            raise CaptureError("box xyxy must contain four normalized coordinates")
        x1, y1, x2, y2 = (_clamp(value) for value in xyxy)
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 0.002 or y2 - y1 < 0.002:
            raise CaptureError("box is too small")
        confidence = item.get("confidence")
        if confidence is not None:
            try:
                confidence = round(min(1.0, max(0.0, float(confidence))), 4)
            except (TypeError, ValueError) as exc:
                raise CaptureError("box confidence must be a number") from exc
        validated.append({
            "box_id": str(item.get("box_id") or uuid.uuid4().hex),
            "class_id": class_id,
            "label": PPE_CLASSES[class_id],
            "xyxy": [round(x1, 6), round(y1, 6), round(x2, 6), round(y2, 6)],
            "confidence": confidence,
        })
    return validated


def _iou(left: list[float], right: list[float]) -> float:
    ix1, iy1 = max(left[0], right[0]), max(left[1], right[1])
    ix2, iy2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = ((left[2] - left[0]) * (left[3] - left[1])
             + (right[2] - right[0]) * (right[3] - right[1]) - intersection)
    return intersection / union if union > 0 else 0.0


def draft_boxes(detections: object, *, width: int, height: int) -> list[dict[str, Any]]:
    """Map edge detections to normalized runtime classes, removing duplicate person boxes."""

    if not isinstance(detections, list) or width <= 0 or height <= 0:
        return []
    candidates: list[dict[str, Any]] = []
    for detection in detections:
        if not isinstance(detection, dict):
            continue
        class_id = _class_id(detection.get("class"))
        raw = detection.get("box")
        if class_id is None or not isinstance(raw, list) or len(raw) != 4:
            continue
        try:
            xyxy = [
                float(raw[0]) / width,
                float(raw[1]) / height,
                float(raw[2]) / width,
                float(raw[3]) / height,
            ]
            confidence = detection.get("confidence")
            box = _validate_boxes([{
                "class_id": class_id,
                "xyxy": xyxy,
                "confidence": confidence,
            }])[0]
        except (CaptureError, TypeError, ValueError):
            continue
        if any(
            existing["class_id"] == class_id and _iou(existing["xyxy"], box["xyxy"]) >= 0.8
            for existing in candidates
        ):
            continue
        candidates.append(box)
    return candidates


class CaptureService:
    def __init__(self, docs: DocStore, *, capture_dir: str, export_dir: str) -> None:
        self.sessions = docs.table(CAPTURE_SESSIONS_TABLE)
        self.frames = docs.table(CAPTURE_FRAMES_TABLE)
        self.capture_dir = Path(capture_dir)
        self.export_dir = Path(export_dir)

    async def create_session(self, *, name: str, split: str) -> dict[str, Any]:
        clean_name = " ".join(name.split()).strip()
        if not clean_name:
            raise CaptureError("session name is required")
        if len(clean_name) > 80:
            raise CaptureError("session name must be 80 characters or fewer")
        if split not in CAPTURE_SPLITS:
            raise CaptureError("split must be train, val, or test")
        now = time.time()
        return await self.sessions.put({
            "session_id": uuid.uuid4().hex,
            "name": clean_name,
            "split": split,
            "created_at": now,
            "updated_at": now,
        })

    async def list_sessions(self) -> list[dict[str, Any]]:
        sessions = await self.sessions.list(limit=500)
        frames = await self.frames.list(limit=10000)
        counts: dict[str, Counter[str]] = {}
        for frame in frames:
            counter = counts.setdefault(str(frame["session_id"]), Counter())
            counter["frames"] += 1
            counter[str(frame.get("review_status", "draft"))] += 1
        return [
            {
                **session,
                "frame_count": counts.get(str(session["session_id"]), Counter())["frames"],
                "reviewed_count": counts.get(str(session["session_id"]), Counter())["reviewed"],
                "draft_count": counts.get(str(session["session_id"]), Counter())["draft"],
            }
            for session in sessions
        ]

    async def capture_frame(
        self,
        *,
        session_id: str,
        jpeg: bytes,
        detections: object,
        source_worker: str,
        zone: str | None,
        captured_at: float | None,
    ) -> dict[str, Any]:
        import cv2
        import numpy as np

        session = await self.sessions.get(session_id)
        if session is None:
            raise CaptureError("capture session not found")
        if not jpeg:
            raise CaptureError("snapshot image is empty")
        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[0] < 2 or frame.shape[1] < 2:
            raise CaptureError("snapshot is not a readable image")
        height, width = int(frame.shape[0]), int(frame.shape[1])
        frame_id = uuid.uuid4().hex
        session_dir = self.capture_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        image_path = session_dir / f"{frame_id}.jpg"
        ok = cv2.imwrite(str(image_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise CaptureError("snapshot could not be written")
        now = time.time()
        record = await self.frames.put({
            "frame_id": frame_id,
            "session_id": session_id,
            "review_status": "draft",
            "captured_at": float(captured_at or now),
            "source_worker": source_worker.strip() or "unknown",
            "zone": (zone or "").strip() or None,
            "created_at": now,
            "updated_at": now,
            "width": width,
            "height": height,
            "image_path": str(image_path.resolve()),
            "boxes": draft_boxes(detections, width=width, height=height),
        })
        await self.sessions.patch(session_id, {"updated_at": now})
        return record

    async def list_frames(self, session_id: str) -> list[dict[str, Any]]:
        if await self.sessions.get(session_id) is None:
            raise CaptureError("capture session not found")
        return await self.frames.list(
            where={"session_id": session_id}, limit=5000, descending=False
        )

    async def update_frame(
        self, frame_id: str, *, boxes: object, review_status: str
    ) -> dict[str, Any]:
        if review_status not in ("draft", "reviewed"):
            raise CaptureError("review_status must be draft or reviewed")
        current = await self.frames.get(frame_id)
        if current is None:
            raise CaptureError("capture frame not found")
        updated = await self.frames.patch(frame_id, {
            "boxes": _validate_boxes(boxes),
            "review_status": review_status,
            "updated_at": time.time(),
        })
        assert updated is not None
        return updated

    async def image_path(self, frame_id: str) -> Path:
        frame = await self.frames.get(frame_id)
        if frame is None:
            raise CaptureError("capture frame not found")
        path = Path(str(frame.get("image_path", "")))
        if not path.is_file() or path.parent.resolve() != (self.capture_dir / frame["session_id"]).resolve():
            raise CaptureError("capture image is unavailable")
        return path

    async def export(self, session_ids: list[str] | None = None) -> dict[str, Any]:
        sessions = await self.sessions.list(limit=500, descending=False)
        if session_ids:
            wanted = set(session_ids)
            sessions = [session for session in sessions if session["session_id"] in wanted]
            missing = wanted - {str(session["session_id"]) for session in sessions}
            if missing:
                raise CaptureError(f"capture session not found: {sorted(missing)[0]}")
        if not sessions:
            raise CaptureError("no capture sessions selected")

        selected = {str(session["session_id"]): session for session in sessions}
        all_frames = await self.frames.list(
            where={"review_status": "reviewed"}, limit=100000, descending=False
        )
        frames = [frame for frame in all_frames if frame["session_id"] in selected]
        if not frames:
            raise CaptureError("no reviewed frames are available to export")
        reviewed_splits = {
            str(selected[str(frame["session_id"])]["split"])
            for frame in frames
        }
        if "train" not in reviewed_splits or "val" not in reviewed_splits:
            raise CaptureError(
                "export requires at least one reviewed frame in train and val sessions"
            )

        export_id = time.strftime("site_ppe_%Y%m%dT%H%M%SZ", time.gmtime())
        output = self.export_dir / export_id
        suffix = 1
        while output.exists():
            output = self.export_dir / f"{export_id}_{suffix}"
            suffix += 1
        for split in CAPTURE_SPLITS:
            (output / "images" / split).mkdir(parents=True, exist_ok=True)
            (output / "labels" / split).mkdir(parents=True, exist_ok=True)

        counts: Counter[str] = Counter()
        boxes_by_class: Counter[str] = Counter()
        for frame in frames:
            split = str(selected[str(frame["session_id"])]["split"])
            source = Path(str(frame["image_path"]))
            if not source.is_file():
                raise CaptureError(f"capture image is unavailable: {frame['frame_id']}")
            stem = f"{frame['session_id'][:10]}_{frame['frame_id']}"
            destination = output / "images" / split / f"{stem}.jpg"
            try:
                destination.hardlink_to(source)
            except OSError:
                shutil.copy2(source, destination)
            lines: list[str] = []
            for box in _validate_boxes(frame.get("boxes", [])):
                x1, y1, x2, y2 = box["xyxy"]
                lines.append(
                    f"{box['class_id']} {(x1 + x2) / 2:.6f} {(y1 + y2) / 2:.6f} "
                    f"{x2 - x1:.6f} {y2 - y1:.6f}"
                )
                boxes_by_class[PPE_CLASSES[box["class_id"]]] += 1
            (output / "labels" / split / f"{stem}.txt").write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
            )
            counts[split] += 1

        data = {
            "path": str(output.resolve()),
            "train": "images/train",
            "val": "images/val",
            "test": "images/test",
            "names": {index: name for index, name in enumerate(PPE_CLASSES)},
        }
        (output / "data.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        summary = {
            "export_id": output.name,
            "output_dir": str(output.resolve()),
            "data_yaml": str((output / "data.yaml").resolve()),
            "images": {split: counts[split] for split in CAPTURE_SPLITS},
            "boxes": dict(boxes_by_class),
            "session_ids": list(selected),
            "note": "Sessions remain intact; no recording session crosses dataset splits.",
        }
        (output / "EXPORT.yaml").write_text(yaml.safe_dump(summary, sort_keys=False), encoding="utf-8")
        return summary
