"""Spoken-phrase generation: audience framing, unit expansion, and total-function guarantees."""

from __future__ import annotations

import pytest

from fieldpilot.alerts.speech import AUDIENCES, spoken_phrase


def alert(**over):
    base = {
        "alert_id": "al-1",
        "event_type": "measurement",
        "severity": "high",
        "zone": "zone A12",
        "worker_id": "w-9",
        "payload": {"element": "rebar_spacing", "deviation_mm": 40.0},
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- the headline promise


def test_worker_hears_the_products_headline_sentence():
    """The exact phrasing the pitch promises the worker hears through the glasses."""

    assert spoken_phrase(alert(), audience="worker") == (
        "Stop work. Rebar spacing is 40 millimetres above spec."
    )


def test_each_audience_gets_its_own_framing_of_one_fact():
    a = alert()
    # The worker is standing in the zone and knows their own id: reading either back is noise.
    worker = spoken_phrase(a, audience="worker")
    assert "zone A12" not in worker and "w-9" not in worker

    # A peer is warned without a colleague's id being read aloud (PRD §4.4).
    peer = spoken_phrase(a, audience="peer")
    assert "in your zone" in peer and "w-9" not in peer

    # The supervisor triages across workers and zones, so both are qualified.
    dash = spoken_phrase(a, audience="dashboard")
    assert "in zone A12" in dash
    assert dash.startswith("High alert.")

    # All three describe the same underlying fact.
    for said in (worker, peer, dash):
        assert "rebar spacing is 40 millimetres above spec" in said.lower()


# --------------------------------------------------------------------------- severity lead-ins


@pytest.mark.parametrize(
    ("severity", "lead"),
    [("critical", "Stop work."), ("high", "Stop work."), ("medium", "Heads up."), ("low", "Note.")],
)
def test_severity_sets_how_urgently_the_worker_is_addressed(severity, lead):
    assert spoken_phrase(alert(severity=severity), audience="worker").startswith(lead)


def test_an_unknown_severity_degrades_to_medium_rather_than_raising():
    assert spoken_phrase(alert(severity="apocalyptic"), audience="worker").startswith("Heads up.")


def test_an_unknown_audience_degrades_to_the_worker_framing():
    assert spoken_phrase(alert(), audience="nobody") == spoken_phrase(alert(), audience="worker")


# --------------------------------------------------------------------------- measurement


def test_a_negative_deviation_is_spoken_as_below_spec():
    said = spoken_phrase(
        alert(payload={"element": "pipe offset", "deviation_mm": -12.0}), audience="worker"
    )
    assert "12 millimetres below spec" in said
    assert "-" not in said  # a synthesiser must never be handed a bare minus sign


def test_deviation_is_derived_when_only_measured_and_spec_are_present():
    said = spoken_phrase(
        alert(payload={"element": "rebar spacing", "measured_mm": 27.5, "spec_mm": 20.0}),
        audience="worker",
    )
    assert "7.5 millimetres above spec" in said


def test_a_measurement_with_no_numbers_still_says_something_useful():
    said = spoken_phrase(alert(payload={"element": "rebar spacing"}), audience="worker")
    assert "outside spec" in said


def test_whole_numbers_are_not_read_out_with_a_decimal_point():
    assert "40 millimetres" in spoken_phrase(alert(), audience="worker")
    assert "40.0" not in spoken_phrase(alert(), audience="worker")


# --------------------------------------------------------------------------- ppe


def test_ppe_is_imperative_for_the_worker_and_descriptive_for_others():
    a = alert(event_type="ppe_missing", payload={"ppe_item": "helmet"})
    assert spoken_phrase(a, audience="worker") == "Stop work. Put on your hard hat."
    assert spoken_phrase(a, audience="peer") == (
        "Heads up. A worker is missing a hard hat in your zone."
    )
    assert spoken_phrase(a, audience="dashboard") == (
        "High alert. Worker w-9 is missing a hard hat in zone A12."
    )


@pytest.mark.parametrize("item", ["gloves", "goggles", "boots"])
def test_plural_ppe_never_gets_a_singular_article(item):
    said = spoken_phrase(alert(event_type="ppe", payload={"ppe_item": item}), audience="dashboard")
    assert " a " not in f" {said} "
    assert "missing" in said


def test_singular_ppe_keeps_its_article():
    said = spoken_phrase(alert(event_type="ppe", payload={"ppe_item": "vest"}), audience="peer")
    assert "missing a high visibility vest" in said


def test_an_unmapped_ppe_item_is_still_spoken():
    said = spoken_phrase(
        alert(event_type="ppe", payload={"ppe_item": "face_shield"}), audience="worker"
    )
    assert "face shield" in said
    assert "_" not in said


# --------------------------------------------------------------------------- other families


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("fall_detected", "fall"),
        ("proximity", "close"),
        ("crack", "structural defect"),
        ("inspection", "structural defect"),
        ("fire", "fire"),
        ("gas", "gas detected"),
        ("unnoticed_hazard", "noticed"),
    ],
)
def test_every_detector_family_has_purpose_written_speech(event_type, expected):
    said = spoken_phrase(alert(event_type=event_type, payload={}), audience="worker")
    # Lowercased because the clause is capitalised when it follows the lead-in's period.
    assert expected in said.lower()
    # Purpose-written speech, not the generic "<event type> was detected" fallback, which would
    # read back the raw type and produce nonsense like "fall detected was detected".
    assert f"{event_type.replace('_', ' ')} was detected" not in said.lower()


def test_a_critical_evacuation_tells_the_worker_what_to_do():
    assert "Evacuate now" in spoken_phrase(
        alert(event_type="fire", severity="critical", payload={}), audience="worker"
    )


def test_proximity_names_the_equipment_when_the_detector_supplies_it():
    said = spoken_phrase(
        alert(event_type="proximity", payload={"kind": "excavator"}), audience="worker"
    )
    assert "close to the excavator" in said
    assert "Step back" in said


# --------------------------------------------------------------------------- robustness


def test_an_unknown_event_type_falls_back_to_the_detector_message():
    said = spoken_phrase(
        alert(event_type="brand_new_detector", message="Something odd at 40mm.", payload={}),
        audience="worker",
    )
    # Units are still expanded, and the message's own period does not strand the location clause.
    assert said == "Stop work. Something odd at 40 millimetres."


def test_the_location_clause_is_never_stranded_as_its_own_fragment():
    said = spoken_phrase(
        alert(event_type="brand_new_detector", message="Something odd.", payload={}),
        audience="peer",
    )
    assert said == "Heads up. Something odd in your zone."
    assert ". In your zone" not in said


@pytest.mark.parametrize("audience", AUDIENCES)
def test_an_empty_alert_still_yields_one_spoken_sentence(audience):
    said = spoken_phrase({}, audience=audience)
    assert said.endswith(".")
    assert said.count(".") == 2  # the lead-in and the clause, nothing dangling
    assert "hazard was detected" in said


@pytest.mark.parametrize(
    "broken",
    [
        {"severity": None, "event_type": None, "payload": None},
        {"event_type": "measurement", "payload": {"deviation_mm": "not-a-number"}},
        {"event_type": "ppe", "payload": {"ppe_item": ""}},
        {"event_type": "measurement", "payload": {"measured_mm": None, "spec_mm": 20}},
        {"zone": 12345, "worker_id": 678},
    ],
)
def test_malformed_alerts_degrade_the_sentence_instead_of_raising(broken):
    """Never lose an alert because we could not phrase it."""

    said = spoken_phrase(broken, audience="worker")
    assert isinstance(said, str) and said.endswith(".")
    assert len(said) > 5


def test_no_underscores_or_double_spaces_ever_reach_a_synthesiser():
    said = spoken_phrase(
        alert(event_type="unnoticed_hazard", payload={"hazard": "moving_vehicle"}),
        audience="dashboard",
    )
    assert "_" not in said
    assert "  " not in said


def test_the_sentence_always_terminates_exactly_once():
    said = spoken_phrase(alert(message="Trailing dots..."), audience="worker")
    assert not said.endswith("..")
