from pathlib import Path

import pytest
import yaml

from scripts.prepare_ppe_dataset import NAMES, Source, prepare


def _sample(root: Path, split: str, name: str, image: bytes, labels: str) -> None:
    image_dir = root / split / "images"
    label_dir = root / split / "labels"
    image_dir.mkdir(parents=True, exist_ok=True)
    label_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / name).write_bytes(image)
    (label_dir / f"{Path(name).stem}.txt").write_text(labels, encoding="utf-8")


def test_prepare_maps_classes_and_keeps_augmentations_in_one_split(tmp_path: Path) -> None:
    css = tmp_path / "css"
    cppe = tmp_path / "cppe"
    _sample(css, "train", "scene.rf.train.jpg", b"train variant", "0 .5 .5 .2 .2\n")
    _sample(css, "valid", "scene.rf.valid.jpg", b"valid variant", "1 .5 .5 .2 .2\n")
    _sample(cppe, "train", "new.jpg", b"new source", "0 .5 .5 .2 .2\n1 .5 .5 .1 .1\n")
    sources = [
        Source(
            key="css",
            root=css,
            splits={"train": "train", "val": "valid"},
            class_map={index: index for index in range(len(NAMES))},
            url="https://example.test/css",
            license="CC BY 4.0",
            roboflow_groups=True,
        ),
        Source(
            key="cppe",
            root=cppe,
            splits={"train": "train"},
            class_map={0: 0},
            url="https://example.test/cppe",
            license="AGPL-3.0",
        ),
    ]

    manifest = prepare(sources, tmp_path / "output")

    assert manifest["images"] == {"val": 2, "train": 1}
    assert manifest["records_moved_to_prevent_split_leakage"] == 1
    assert manifest["boxes"]["train"] == {"Hardhat": 1}
    assert manifest["boxes"]["val"] == {"Hardhat": 1, "Mask": 1}
    assert len(list((tmp_path / "output" / "images" / "train").iterdir())) == 1
    assert len(list((tmp_path / "output" / "images" / "val").iterdir())) == 2
    data = yaml.safe_load((tmp_path / "output" / "data.yaml").read_text(encoding="utf-8"))
    assert data["path"] == str((tmp_path / "output").resolve())


def test_prepare_refuses_to_overwrite_generated_dataset(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="output is not empty"):
        prepare([], output)
