"""The checkpoint registry: pinned entries, and downloads that refuse to install bad bytes.

Hermetic — no network, no downloads, no ultralytics. `registry._urlopen` is the single seam for
network I/O and every test here monkeypatches it, so a regression that reintroduces a real fetch
fails loudly instead of quietly hitting Hugging Face.

What is actually being defended:
  * the pinning invariant — a downloadable PPE entry without a digest is an unverifiable download;
  * `capability`, because running PPE compliance against a person-only detector invents violations;
  * that a checksum mismatch or an oversized response leaves *nothing* behind, not a bad `.pt`.
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

from fieldpilot.models_registry import registry

REPO_ROOT = Path(__file__).resolve().parent.parent

PAYLOAD = b"pretend-checkpoint-bytes" * 64
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


# ---------------------------------------------------------------- fake network

class _FakeResponse(io.BytesIO):
    """Just enough of an `http.client.HTTPResponse`: a context manager with `.read(n)`."""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _serve(payload: bytes):
    """An opener that returns `payload`, and records that it was called."""

    calls: list[str] = []

    def opener(request, timeout=None):  # noqa: ARG001 — signature mirrors urlopen
        calls.append(request.full_url)
        return _FakeResponse(payload)

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def opener(request, timeout=None):  # noqa: ARG001
        raise AssertionError(f"no download expected, but {request.full_url} was fetched")

    monkeypatch.setattr(registry, "_urlopen", opener)


@pytest.fixture
def fake_entry(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A registry entry usable in tests, without touching the real pinned ones."""

    option = {
        "weights": "fake_test_model.pt",
        "label": "Fake Test Model",
        "notes": "Exists only inside this test module.",
        "capability": "ppe",
        "license": "MIT",
        "size_mb": len(PAYLOAD) / (1024 * 1024),
        "url": "https://example.invalid/fake/best.pt",
        "sha256": PAYLOAD_SHA,
    }
    monkeypatch.setitem(registry.MODEL_OPTIONS, "fake", option)
    return option


# ---------------------------------------------------------------- registry shape

def test_every_entry_declares_the_required_fields_and_a_real_capability():
    assert registry.MODEL_OPTIONS, "an empty registry would make the model picker useless"
    for key, option in registry.MODEL_OPTIONS.items():
        missing = [f for f in registry.REQUIRED_FIELDS if not option.get(f)]
        assert not missing, f"{key} is missing {missing}"
        assert option["capability"] in registry.CAPABILITIES, \
            f"{key} declares capability {option['capability']!r}; PPE gating only understands " \
            f"{sorted(registry.CAPABILITIES)}"
        assert option["weights"].endswith(".pt")
        assert "/" not in option["weights"], "weights is a filename inside models_dir, not a path"


def test_a_downloadable_entry_pins_both_a_digest_and_a_size():
    """No digest => unverifiable bytes. No size => no ceiling on a runaway response."""

    downloadable = {k: o for k, o in registry.MODEL_OPTIONS.items() if o.get("url")}
    assert downloadable, "the ported PPE checkpoints must remain downloadable"
    for key, option in downloadable.items():
        assert option["url"].startswith("https://"), key
        sha = option.get("sha256")
        assert sha and re.fullmatch(r"[0-9a-f]{64}", sha), f"{key} has no valid pinned sha256"
        assert option.get("size_mb", 0) > 0, f"{key} declares no size, so it gets no ceiling"


def test_hosted_urls_pin_an_immutable_revision_not_a_branch():
    """`/resolve/main/` can change under us, which would silently invalidate the pinned digest."""

    for key, option in registry.MODEL_OPTIONS.items():
        url = option.get("url")
        if not url or "huggingface.co" not in url:
            continue
        revision = re.search(r"/resolve/([^/]+)/", url)
        assert revision, f"{key}: unrecognised Hugging Face URL shape"
        assert re.fullmatch(r"[0-9a-f]{40}", revision.group(1)), \
            f"{key} pins {revision.group(1)!r}; pin a commit hash instead"


def test_the_ported_ppe_checkpoints_and_person_detectors_are_all_present():
    for key in (
        "ppe_css", "ppe_construction_n", "ppe_helmet_vest_n", "ppe_safetyvision_s", "ppe_vyra_m",
    ):
        assert registry.MODEL_OPTIONS[key]["capability"] == "ppe"
    for key in ("yolo26n", "yolo26s", "yolo11n"):
        assert registry.MODEL_OPTIONS[key]["capability"] == "person"
    assert registry.MODEL_OPTIONS["custom"]["capability"] == "ppe"
    # the configured default must be selectable, at the filename config.yaml points to
    assert registry.MODEL_OPTIONS["ppe_css"]["weights"] == "ppe_css.pt"


def test_default_models_dir_is_the_repo_weights_directory():
    assert registry.DEFAULT_MODELS_DIR == REPO_ROOT / "models"


def test_get_option_hands_back_a_copy_and_None_for_strangers():
    assert registry.get_option("nope") is None
    option = registry.get_option("ppe_css")
    assert option is not None and option["capability"] == "ppe"
    option["capability"] = "person"
    assert registry.MODEL_OPTIONS["ppe_css"]["capability"] == "ppe", \
        "a caller mutating a returned dict must not be able to disable PPE gating globally"


def test_unknown_key_raises_everywhere_it_can():
    for call in (
        lambda: registry.weights_path("nope"),
        lambda: registry.verify_local_sync("nope"),
        lambda: registry.ensure_weights_sync("nope"),
    ):
        with pytest.raises(registry.ModelRegistryError, match="Unknown model key"):
            call()


# ---------------------------------------------------------------- listing

def test_list_models_reports_downloaded_and_path(tmp_path: Path):
    present = tmp_path / registry.MODEL_OPTIONS["ppe_css"]["weights"]
    present.write_bytes(PAYLOAD)

    rows = {row["key"]: row for row in registry.list_models(tmp_path)}
    assert set(rows) == set(registry.MODEL_OPTIONS)

    assert rows["ppe_css"]["downloaded"] is True
    assert rows["ppe_css"]["path"] == str(present)
    assert rows["ppe_css"]["size_bytes"] == len(PAYLOAD)

    absent = rows["ppe_vyra_m"]
    assert absent["downloaded"] is False and absent["size_bytes"] is None
    assert absent["path"] == str(tmp_path / "ppe_vyra_m.pt")
    # the descriptive fields the picker renders survive the round trip
    assert absent["label"] and absent["notes"] and absent["license"] and absent["capability"]


def test_list_models_rows_do_not_alias_the_registry(tmp_path: Path):
    rows = registry.list_models(tmp_path)
    rows[0]["sha256"] = "tampered"
    assert registry.MODEL_OPTIONS[rows[0]["key"]].get("sha256") != "tampered"


# ---------------------------------------------------------------- ensure_weights

async def test_ensure_weights_is_a_noop_when_a_valid_file_already_exists(
    tmp_path: Path, fake_entry: dict, monkeypatch: pytest.MonkeyPatch
):
    destination = tmp_path / fake_entry["weights"]
    destination.write_bytes(PAYLOAD)
    _forbid_network(monkeypatch)

    assert await registry.ensure_weights("fake", tmp_path) == destination
    assert destination.read_bytes() == PAYLOAD


async def test_ensure_weights_downloads_verifies_and_installs(
    tmp_path: Path, fake_entry: dict, monkeypatch: pytest.MonkeyPatch
):
    opener = _serve(PAYLOAD)
    monkeypatch.setattr(registry, "_urlopen", opener)

    path = await registry.ensure_weights("fake", tmp_path)
    assert path.read_bytes() == PAYLOAD
    assert opener.calls == [fake_entry["url"]]
    assert list(tmp_path.glob("*.part")) == []

    # second call is idempotent: no further fetch
    _forbid_network(monkeypatch)
    assert await registry.ensure_weights("fake", tmp_path) == path


async def test_force_redownloads_even_when_the_file_verifies(
    tmp_path: Path, fake_entry: dict, monkeypatch: pytest.MonkeyPatch
):
    destination = tmp_path / fake_entry["weights"]
    destination.write_bytes(PAYLOAD)
    opener = _serve(PAYLOAD)
    monkeypatch.setattr(registry, "_urlopen", opener)

    await registry.ensure_weights("fake", tmp_path, force=True)
    assert opener.calls == [fake_entry["url"]]


async def test_checksum_mismatch_raises_and_leaves_no_checkpoint_behind(
    tmp_path: Path, fake_entry: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(registry, "_urlopen", _serve(b"substituted-weights" * 64))
    destination = tmp_path / fake_entry["weights"]

    with pytest.raises(registry.ModelRegistryError, match="checksum verification failed"):
        await registry.ensure_weights("fake", tmp_path)

    assert not destination.exists(), "a checkpoint that failed verification must not be installed"
    assert list(tmp_path.glob("*.part")) == [], "the temp file must be cleaned up"


async def test_a_corrupt_existing_file_is_deleted_and_not_reported_as_usable(
    tmp_path: Path, fake_entry: dict, monkeypatch: pytest.MonkeyPatch
):
    destination = tmp_path / fake_entry["weights"]
    destination.write_bytes(b"corrupted on disk")
    monkeypatch.setattr(registry, "_urlopen", _serve(b"still-wrong" * 64))

    with pytest.raises(registry.ModelRegistryError, match="checksum verification failed"):
        await registry.ensure_weights("fake", tmp_path)
    assert not destination.exists(), \
        "the stale mismatching file must be gone, or the next load silently uses bad weights"


async def test_a_response_beyond_the_declared_size_is_aborted(
    tmp_path: Path, fake_entry: dict, monkeypatch: pytest.MonkeyPatch
):
    """A hostile or misconfigured URL must not be able to fill the disk."""

    ceiling_mb = 0.01
    monkeypatch.setitem(registry.MODEL_OPTIONS, "fake", {**fake_entry, "size_mb": ceiling_mb})
    monkeypatch.setattr(registry, "_urlopen", _serve(b"x" * 5_000_000))

    with pytest.raises(registry.ModelRegistryError, match="exceeded its declared size"):
        await registry.ensure_weights("fake", tmp_path)

    assert list(tmp_path.glob("*.part")) == [], "the aborted partial download must be removed"
    assert not (tmp_path / fake_entry["weights"]).exists()


async def test_the_custom_slot_explains_itself_when_the_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _forbid_network(monkeypatch)
    with pytest.raises(registry.ModelRegistryError) as excinfo:
        await registry.ensure_weights("custom", tmp_path)

    message = str(excinfo.value)
    assert str(tmp_path / "custom_ppe.pt") in message, "the error must name the path to fill"
    assert "never downloaded" in message and "fine-tuned" in message


async def test_a_person_detector_without_a_url_points_at_the_way_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _forbid_network(monkeypatch)
    with pytest.raises(registry.ModelRegistryError) as excinfo:
        await registry.ensure_weights("yolo11n", tmp_path)
    assert "yolo11n.pt" in str(excinfo.value)


async def test_an_unpinned_present_file_is_accepted_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _forbid_network(monkeypatch)
    (tmp_path / "yolo11n.pt").write_bytes(PAYLOAD)
    assert await registry.ensure_weights("yolo11n", tmp_path) == tmp_path / "yolo11n.pt"


# ---------------------------------------------------------------- verify_local

async def test_verify_local_verdicts_for_present_absent_and_corrupt(
    tmp_path: Path, fake_entry: dict
):
    ok, detail = await registry.verify_local("fake", tmp_path)
    assert ok is False and "does not exist" in detail

    destination = tmp_path / fake_entry["weights"]
    destination.write_bytes(PAYLOAD)
    ok, detail = await registry.verify_local("fake", tmp_path)
    assert ok is True and PAYLOAD_SHA in detail

    destination.write_bytes(PAYLOAD + b"tampered")
    ok, detail = await registry.verify_local("fake", tmp_path)
    assert ok is False and "digest mismatch" in detail
    assert destination.exists(), "verify_local only reports; deleting is ensure_weights' job"


async def test_verify_local_says_so_when_there_is_no_digest_to_check(tmp_path: Path):
    (tmp_path / "yolo11n.pt").write_bytes(PAYLOAD)
    ok, detail = await registry.verify_local("yolo11n", tmp_path)
    assert ok is True and "no pinned digest" in detail


def test_file_sha256_matches_hashlib(tmp_path: Path):
    path = tmp_path / "blob.bin"
    path.write_bytes(PAYLOAD)
    assert registry.file_sha256(path) == PAYLOAD_SHA


# ---------------------------------------------------------------- import weight

def test_importing_the_registry_pulls_in_no_ml_stack():
    """A FastAPI handler listing models must not pay for torch, and tests must stay fast."""

    probe = (
        "import sys; import fieldpilot.models_registry as m; "
        "assert m.MODEL_OPTIONS; "
        "heavy = [n for n in ('torch', 'ultralytics', 'cv2', 'numpy') if n in sys.modules]; "
        "assert not heavy, heavy"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
