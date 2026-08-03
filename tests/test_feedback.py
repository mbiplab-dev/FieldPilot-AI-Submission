"""Supervisor feedback: recording decisions, filtering, stats and the training claim/release."""

from __future__ import annotations

import pytest

from fieldpilot.feedback.service import FEEDBACK_TABLE, FeedbackService
from fieldpilot.storage import DocStore


@pytest.fixture
async def svc(tmp_path):
    store = DocStore("sqlite", str(tmp_path / "feedback.db"))
    await store.start([FEEDBACK_TABLE])
    try:
        yield FeedbackService(store)
    finally:
        await store.stop()


def make_alert(**over):
    base = {
        "alert_id": "al-1",
        "event_type": "ppe",
        "zone": "zone-a",
        "worker_id": "w-7",
        "severity": "high",
        "message": "No helmet on w-7",
        "hit_count": 3,
        "confidence": 0.91,
        "image_path": "/frames/al-1.jpg",
        "image_url": "/media/al-1.jpg",
        "payload": {"event_id": "ev-1", "bbox": [10.0, 20.0, 110.0, 220.0]},
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- record


async def test_record_stores_the_decision_and_provenance(svc):
    stored = await svc.record(
        alert=make_alert(), decision="approve", label="NO-Hardhat",
        reviewer="site-manager", notes="clear violation",
    )
    assert stored["decision"] == "approve"
    assert stored["label"] == "NO-Hardhat"
    assert stored["alert_id"] == "al-1"
    assert stored["event_id"] == "ev-1"
    assert stored["event_type"] == "ppe"
    assert stored["image_path"] == "/frames/al-1.jpg"
    assert stored["zone"] == "zone-a"
    assert stored["worker_id"] == "w-7"
    assert stored["reviewer"] == "site-manager"
    assert stored["notes"] == "clear violation"
    assert stored["confidence"] == 0.91
    assert stored["consumed_at"] is None
    assert stored["consumed_by"] is None
    assert len(stored["feedback_id"]) == 32

    persisted = await svc.for_alert("al-1")
    assert persisted["feedback_id"] == stored["feedback_id"]
    assert persisted["decision"] == "approve"
    assert persisted["confidence"] == 0.91


async def test_record_normalises_the_decision_case(svc):
    stored = await svc.record(alert=make_alert(), decision="REJECT")
    assert stored["decision"] == "reject"


async def test_record_rejects_an_invalid_decision(svc):
    with pytest.raises(ValueError):
        await svc.record(alert=make_alert(), decision="maybe")
    assert await svc.list() == []


async def test_record_leaves_the_label_unset_when_no_detector_class_is_known(svc):
    """The label must name a class the detector predicts, not the event family.

    Defaulting to `event_type` ("fall", "ppe") produced rows the dataset builder silently
    discarded, so an unresolvable label stays None and is skipped visibly instead.
    """

    stored = await svc.record(alert=make_alert(event_type="fall"), decision="approve")
    assert stored["label"] is None


async def test_record_takes_the_label_from_the_detector_class(svc):
    alert = make_alert(event_type="ppe")
    alert["payload"] = {**alert.get("payload", {}), "class": "NO-Hardhat"}
    stored = await svc.record(alert=alert, decision="approve")
    assert stored["label"] == "NO-Hardhat"


async def test_explicit_label_overrides_the_detector_class(svc):
    alert = make_alert(event_type="ppe")
    alert["payload"] = {**alert.get("payload", {}), "class": "NO-Hardhat"}
    stored = await svc.record(alert=alert, decision="approve", label="NO-Safety Vest")
    assert stored["label"] == "NO-Safety Vest"


async def test_bbox_and_other_undeclared_fields_round_trip(svc):
    stored = await svc.record(
        alert=make_alert(), decision="approve", bbox=[1.5, 2.5, 3.5, 4.5]
    )
    assert stored["bbox"] == [1.5, 2.5, 3.5, 4.5]
    reloaded = await svc.for_alert("al-1")
    assert reloaded["bbox"] == [1.5, 2.5, 3.5, 4.5]
    assert reloaded["image_url"] == "/media/al-1.jpg"
    assert reloaded["alert_snapshot"] == {
        "severity": "high", "message": "No helmet on w-7", "hit_count": 3,
    }


async def test_bbox_falls_back_to_the_alert_payload(svc):
    stored = await svc.record(alert=make_alert(), decision="approve")
    assert stored["bbox"] == [10.0, 20.0, 110.0, 220.0]
    assert (await svc.for_alert("al-1"))["bbox"] == [10.0, 20.0, 110.0, 220.0]


async def test_record_tolerates_a_sparse_alert(svc):
    stored = await svc.record(alert={"alert_id": "al-9"}, decision="reject")
    assert stored["confidence"] == 0.0
    assert stored["event_id"] is None
    assert stored["label"] is None
    assert stored["bbox"] is None
    reloaded = await svc.for_alert("al-9")
    assert reloaded["decision"] == "reject"
    assert reloaded["bbox"] is None


async def test_image_path_falls_back_to_the_payload(svc):
    alert = make_alert(image_path=None)
    alert["payload"]["image_path"] = "/frames/from-payload.jpg"
    stored = await svc.record(alert=alert, decision="approve")
    assert stored["image_path"] == "/frames/from-payload.jpg"


# --------------------------------------------------------------------------- list


async def test_list_filters(svc):
    await svc.record(alert=make_alert(alert_id="a1", event_type="ppe"), decision="approve")
    await svc.record(alert=make_alert(alert_id="a2", event_type="fall"), decision="approve")
    await svc.record(alert=make_alert(alert_id="a3", event_type="ppe"), decision="reject")

    assert len(await svc.list()) == 3
    assert {r["alert_id"] for r in await svc.list(decision="approve")} == {"a1", "a2"}
    assert {r["alert_id"] for r in await svc.list(decision="reject")} == {"a3"}
    assert {r["alert_id"] for r in await svc.list(event_type="ppe")} == {"a1", "a3"}
    assert {r["alert_id"] for r in await svc.list(alert_id="a2")} == {"a2"}
    assert {r["alert_id"] for r in await svc.list(decision="approve", event_type="ppe")} == {"a1"}
    assert await svc.list(event_type="crack") == []


async def test_list_unconsumed_only(svc):
    for i in range(3):
        await svc.record(alert=make_alert(alert_id=f"a{i}"), decision="approve")
    assert len(await svc.list(unconsumed_only=True)) == 3

    await svc.claim_for_training("run-1")
    assert await svc.list(unconsumed_only=True) == []
    assert len(await svc.list()) == 3


async def test_list_respects_limit(svc):
    for i in range(5):
        await svc.record(alert=make_alert(alert_id=f"a{i}"), decision="approve")
    assert len(await svc.list(limit=2)) == 2


async def test_for_alert_returns_none_when_unreviewed(svc):
    assert await svc.for_alert("never-reviewed") is None


# --------------------------------------------------------------------------- stats


async def test_stats_on_an_empty_table_has_no_approval_rate(svc):
    assert await svc.stats() == {
        "approved": 0, "rejected": 0, "total": 0, "unconsumed": 0, "approval_rate": None,
    }


async def test_stats_counts_and_approval_rate(svc):
    for i in range(3):
        await svc.record(alert=make_alert(alert_id=f"ok{i}"), decision="approve")
    await svc.record(alert=make_alert(alert_id="bad0"), decision="reject")

    assert await svc.stats() == {
        "approved": 3, "rejected": 1, "total": 4, "unconsumed": 4, "approval_rate": 0.75,
    }


async def test_stats_unconsumed_drops_as_runs_claim_samples(svc):
    for i in range(4):
        await svc.record(alert=make_alert(alert_id=f"a{i}"), decision="approve")
    await svc.claim_for_training("run-1", limit=2)
    stats = await svc.stats()
    assert stats["unconsumed"] == 2
    assert stats["total"] == 4
    assert stats["approval_rate"] == 1.0


# --------------------------------------------------------------------------- claim / release


async def test_claim_stamps_the_run_and_is_not_repeatable(svc):
    for i in range(3):
        await svc.record(alert=make_alert(alert_id=f"a{i}"), decision="approve")

    claimed = await svc.claim_for_training("run-1")
    assert len(claimed) == 3
    assert {r["consumed_by"] for r in claimed} == {"run-1"}
    assert all(isinstance(r["consumed_at"], float) and r["consumed_at"] > 0 for r in claimed)

    assert await svc.claim_for_training("run-2") == []
    # the stamps survive a round trip, so the run's dataset stays reconstructable
    persisted = await svc.list()
    assert {r["consumed_by"] for r in persisted} == {"run-1"}


async def test_claim_respects_its_limit(svc):
    for i in range(5):
        await svc.record(alert=make_alert(alert_id=f"a{i}"), decision="approve")
    first = await svc.claim_for_training("run-1", limit=2)
    assert len(first) == 2
    second = await svc.claim_for_training("run-2", limit=10)
    assert len(second) == 3
    assert {r["consumed_by"] for r in second} == {"run-2"}


async def test_claim_preserves_the_sample_payload(svc):
    await svc.record(alert=make_alert(), decision="approve", bbox=[1.0, 2.0, 3.0, 4.0])
    claimed = await svc.claim_for_training("run-1")
    assert claimed[0]["bbox"] == [1.0, 2.0, 3.0, 4.0]
    assert claimed[0]["image_path"] == "/frames/al-1.jpg"
    assert claimed[0]["decision"] == "approve"


async def test_release_makes_a_failed_runs_samples_claimable_again(svc):
    for i in range(3):
        await svc.record(alert=make_alert(alert_id=f"a{i}"), decision="approve")
    await svc.claim_for_training("run-1")

    assert await svc.release("run-1") == 3
    released = await svc.list()
    assert {r["consumed_by"] for r in released} == {None}
    assert {r["consumed_at"] for r in released} == {None}
    assert (await svc.stats())["unconsumed"] == 3

    reclaimed = await svc.claim_for_training("run-2")
    assert len(reclaimed) == 3
    assert {r["consumed_by"] for r in reclaimed} == {"run-2"}


async def test_release_only_touches_its_own_run(svc):
    await svc.record(alert=make_alert(alert_id="a0"), decision="approve")
    await svc.claim_for_training("run-1", limit=1)
    await svc.record(alert=make_alert(alert_id="a1"), decision="approve")
    await svc.claim_for_training("run-2", limit=1)

    assert await svc.release("run-1") == 1
    remaining = {r["alert_id"]: r["consumed_by"] for r in await svc.list()}
    assert remaining == {"a0": None, "a1": "run-2"}


async def test_release_of_an_unknown_run_is_a_no_op(svc):
    await svc.record(alert=make_alert(), decision="approve")
    assert await svc.release("never-ran") == 0
    assert (await svc.stats())["unconsumed"] == 1
