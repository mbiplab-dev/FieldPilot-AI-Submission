"""Worker assistant routing, authority boundaries, and calibrated measurement."""

from __future__ import annotations

import pytest

from fieldpilot.assistant import AssistantService, MeasurementError, measure_from_points


async def test_measurement_request_opens_tool_without_calling_a_model():
    assistant = AssistantService(enabled=False)
    reply = await assistant.ask("FieldPilot, measure this rebar spacing")
    assert reply.intent == "measure"
    assert reply.action == {"type": "open_measurement"}
    assert "same plane" in reply.answer


async def test_hazard_report_requires_worker_confirmation():
    assistant = AssistantService(enabled=False)
    reply = await assistant.ask("Report smoke near the stairs")
    assert reply.intent == "report"
    assert reply.requires_confirmation is True
    assert reply.action["event_type"] == "fire"
    assert reply.action["severity"] == "critical"


async def test_identification_without_a_photo_requests_one():
    assistant = AssistantService(enabled=False)
    reply = await assistant.ask("What is this tool?")
    assert reply.intent == "identify"
    assert reply.action == {"type": "capture_photo"}


async def test_disabled_model_degrades_safely():
    assistant = AssistantService(enabled=False)
    reply = await assistant.ask("Can I continue working here?")
    assert reply.degraded is True
    assert "Stop work" in reply.answer
    assert reply.model is None


def test_point_measurement_and_spec_check():
    result = measure_from_points(
        reference_points=[[10, 10], [210, 10]],
        measurement_points=[[20, 50], [220, 50]],
        reference_mm=100,
        spec_mm=90,
        tolerance_mm=5,
        image_size=[640, 480],
    )
    assert result["measured_mm"] == 100
    assert result["deviation_mm"] == 10
    assert result["within_tolerance"] is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reference_points": [[1, 1], [1, 1]]}, "too close"),
        ({"measurement_points": [[2, 2], [2, 2]]}, "too close"),
        ({"reference_mm": 0}, "greater than zero"),
        ({"measurement_points": [[2, 2], [900, 2]]}, "inside the image"),
    ],
)
def test_invalid_measurements_are_rejected(kwargs, message):
    values = {
        "reference_points": [[10, 10], [110, 10]],
        "measurement_points": [[20, 20], [120, 20]],
        "reference_mm": 100,
        "image_size": [640, 480],
    }
    values.update(kwargs)
    with pytest.raises(MeasurementError, match=message):
        measure_from_points(**values)
