"""Weights + val-set plumbing: PPE fails loudly, checkpoints are verified, the gate is unblocked.

None of these tests touch the network. The point they defend is that a missing weights file
produces a *stated* reason instead of a silently dead detector, and that the demo val set really
satisfies `LearningService._check_val_set` rather than merely looking like it does.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

from fieldpilot.learning.service import LearningService
from fieldpilot.safety.ppe import PPEChecker
from tests.conftest import make_cfg

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    """Import a file from scripts/ (not a package) by path."""

    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch_models = _load_script("fetch_models")
make_val_set = _load_script("make_val_set")


# ---------------------------------------------------------------- PPE fails loudly

def test_missing_ppe_model_warns_with_path_remedy_and_consequence(caplog, tmp_path):
    missing = tmp_path / "models" / "ppe_css.pt"
    with caplog.at_level(logging.WARNING, logger="fieldpilot.safety.ppe"):
        checker = PPEChecker(make_cfg(detection={"ppe_model": str(missing)}))

    assert checker.enabled is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a configured-but-missing PPE model must WARN, not fail silently"
    text = warnings[0].getMessage()
    assert str(missing) in text                      # the path tried
    assert "does not exist" in text                  # missing, not merely unloadable
    assert "make fetch-models" in text               # the remedy
    assert "PPE violation detection is DISABLED" in text and "hardhat/vest" in text


def test_unconfigured_ppe_model_is_informational_not_a_warning(caplog):
    with caplog.at_level(logging.INFO, logger="fieldpilot.safety.ppe"):
        checker = PPEChecker(make_cfg(detection={"ppe_model": None}))

    assert checker.enabled is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "ppe_model: null is a legitimate choice — it must not be reported as a misconfiguration"
    assert any("not configured" in r.getMessage() for r in caplog.records)


def test_unloadable_ppe_file_is_distinguished_from_a_missing_one(tmp_path):
    junk = tmp_path / "ppe_css.pt"
    junk.write_bytes(b"this is not a torch checkpoint")
    checker = PPEChecker(make_cfg(detection={"ppe_model": str(junk)}))

    assert checker.enabled is False
    reason = checker.status["reason"]
    assert isinstance(reason, str) and "failed to load" in reason
    assert "does not exist" not in reason


def test_status_describes_why_ppe_is_off_and_cannot_be_mutated(tmp_path):
    missing = tmp_path / "nope.pt"
    checker = PPEChecker(make_cfg(detection={"ppe_model": str(missing)}))

    status = checker.describe()
    assert set(status) == {"enabled", "model", "reason"}
    assert status == {"enabled": False, "model": str(missing), "reason": checker.status["reason"]}

    status["enabled"] = True  # a health endpoint fiddling with the dict must not change our state
    assert checker.status["enabled"] is False

    with pytest.raises(AttributeError):
        checker.status = {}  # type: ignore[misc]


def test_disabled_ppe_checker_stays_out_of_the_safety_loop():
    """The deliberate design: a PPE failure never propagates into the pipeline."""

    from tests.conftest import make_person, make_result

    checker = PPEChecker(make_cfg(detection={"ppe_model": "/no/such/model.pt"}))
    result = make_result(0.0, 0, [make_person(1, shoulder_y=120, hip_y=220)])
    assert checker.update(result) == []
    assert checker.last_boxes == [] and checker.equipment_boxes == []


# ---------------------------------------------------------------- fetch_models verification

def test_verify_rejects_a_truncated_or_html_file(tmp_path):
    html = tmp_path / "ppe_css.pt"
    html.write_bytes(b"<!DOCTYPE html><html>404 not found</html>")
    ok, detail, names = fetch_models.verify_checkpoint(html, min_bytes=2_000_000)
    assert ok is False and names == {}
    assert "implausibly small" in detail

    big_html = tmp_path / "big.pt"
    big_html.write_bytes(b"<!DOCTYPE html>" + b"x" * 2_100_000)
    ok, detail, _ = fetch_models.verify_checkpoint(big_html, min_bytes=2_000_000)
    assert ok is False and "not a torch checkpoint" in detail


def test_verify_reports_a_missing_file(tmp_path):
    ok, detail, _ = fetch_models.verify_checkpoint(tmp_path / "absent.pt", min_bytes=1)
    assert ok is False and "does not exist" in detail


def test_specs_are_coherent_and_never_invent_a_url():
    keys = [s.key for s in fetch_models.SPECS]
    assert keys == ["pose", "ppe", "damage"]
    for spec in fetch_models.SPECS:
        assert spec.filename.endswith(".pt") and spec.min_bytes > 0
        assert all(u.startswith("https://") for u in spec.urls)
        # a model with no reachable download must carry manual instructions instead of a guess
        assert spec.urls or spec.manual, f"{spec.key} has neither a URL nor instructions"
    ppe = next(s for s in fetch_models.SPECS if s.key == "ppe")
    assert ppe.filename == "ppe_css.pt", "must land where config.yaml's detection.ppe_model points"
    damage = next(s for s in fetch_models.SPECS if s.key == "damage")
    assert damage.urls == (), "there is no public source for the damage weights — do not fake one"


def test_a_model_with_no_source_is_reported_as_unavailable(tmp_path, capsys):
    spec = fetch_models.ModelSpec(
        key="damage", filename="structural_damage_best.pt", what="x",
        min_bytes=1, urls=(), manual=("copy it from a teammate",),
    )
    assert fetch_models.ensure(spec, tmp_path, force=False, tty=False) is False
    out = capsys.readouterr().out
    assert "MISSING" in out and "copy it from a teammate" in out


# ---------------------------------------------------------------- demo val set

def test_generated_val_set_satisfies_the_learning_gate(tmp_path):
    out = tmp_path / "val_set"
    assert make_val_set.main([
        "--out", str(out), "--n", "6", "--classes", "Hardhat,NO-Hardhat", "--from-model", "",
    ]) == 0

    service = LearningService.__new__(LearningService)
    service.val_set = str(out)
    ok, msg = service._check_val_set()
    assert ok, msg

    images = sorted((out / "images").glob("*.jpg"))
    labels = sorted((out / "labels").glob("*.txt"))
    assert len(images) == len(labels) == 6
    assert [p.stem for p in images] == [p.stem for p in labels]

    for label in labels:
        lines = label.read_text().split()
        assert lines, f"{label} is empty — the val set must carry ground truth"
        for line in label.read_text().strip().splitlines():
            cls, cx, cy, w, h = line.split()
            assert int(cls) in (0, 1), "class ids must stay inside the requested label space"
            assert all(0.0 < float(v) <= 1.0 for v in (cx, cy, w, h))


def test_val_set_is_deterministic_and_labelled_as_demo_data(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    make_val_set.main(["--out", str(a), "--n", "3", "--seed", "7", "--from-model", ""])
    make_val_set.main(["--out", str(b), "--n", "3", "--seed", "7", "--from-model", ""])
    assert (a / "labels" / "val_000000.txt").read_text() == \
           (b / "labels" / "val_000000.txt").read_text()
    assert (a / "images" / "val_000000.jpg").read_bytes() == \
           (b / "images" / "val_000000.jpg").read_bytes()

    readme = (a / "README.md").read_text()
    assert "SYNTHETIC DEMONSTRATION DATA" in readme
    assert "immutable" in readme and "Replacing it with a real locked validation set" in readme
    assert (a / make_val_set.DEMO_MARKER).is_file()


def test_generator_refuses_to_clobber_an_unmarked_val_set(tmp_path):
    out = tmp_path / "real"
    (out / "images").mkdir(parents=True)
    (out / "images" / "site_0001.jpg").write_bytes(b"pretend real frame")

    with pytest.raises(SystemExit) as excinfo:
        make_val_set.main(["--out", str(out), "--n", "2", "--from-model", ""])
    assert "REAL locked validation set" in str(excinfo.value)
    assert (out / "images" / "site_0001.jpg").read_bytes() == b"pretend real frame"

    # --force is the explicit opt-out
    assert make_val_set.main(
        ["--out", str(out), "--n", "2", "--from-model", "", "--force"]
    ) == 0
