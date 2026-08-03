"""Rules Engine: condition DSL, cooldowns, templating, and the three spec exemplars."""

from __future__ import annotations

from fieldpilot.events.schema import Event, EventType, Severity
from fieldpilot.rules.engine import Rule, RuleEngine, default_rules


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def ppe_event(worker="w-1", item="helmet", zone="zone-a") -> Event:
    return Event(
        worker_id=worker, camera_id="cam-1", zone=zone, event_type=EventType.PPE,
        confidence=0.9, severity=Severity.MEDIUM,
        payload={"ppe_item": item, "dedup_key": item},
    )


def crack_event(severity_score: float, zone="zone-b") -> Event:
    return Event(
        worker_id=None, camera_id="cam-2", zone=zone, event_type=EventType.CRACK,
        confidence=0.95, severity=Severity.HIGH,
        payload={"severity_score": severity_score, "dedup_key": "crack-1"},
    )


def measurement_event(element="rebar_spacing", deviation_mm=25.0, zone="zone-c") -> Event:
    return Event(
        worker_id=None, camera_id="cam-3", zone=zone, event_type=EventType.MEASUREMENT,
        confidence=0.9, severity=Severity.MEDIUM,
        payload={"element": element, "deviation_mm": deviation_mm, "dedup_key": element},
    )


# --------------------------------------------------------------------------- condition DSL


def test_all_operators():
    rule = Rule.from_dict({
        "name": "ops",
        "event_types": ["measurement"],
        "conditions": [
            {"field": "event.payload.deviation_mm", "op": "gt", "value": 20},
            {"field": "event.payload.element", "op": "ne", "value": "pipe_diameter"},
            {"field": "event.confidence", "op": "gte", "value": 0.9},
            {"field": "event.zone", "op": "in", "value": ["zone-c", "zone-d"]},
            {"field": "event.payload.element", "op": "contains", "value": "rebar"},
            {"field": "event.payload.element", "op": "exists"},
            {"field": "context.anything", "op": "truthy"},
        ],
        "action": {"type": "create_alert"},
    })
    engine = RuleEngine([rule])
    actions = engine.evaluate(measurement_event(), context={"anything": 1})
    assert len(actions) == 1
    # flip one condition to failing → no action
    actions = engine.evaluate(measurement_event(deviation_mm=5.0), context={"anything": 1})
    assert actions == []


def test_missing_field_fails_closed():
    rule = Rule.from_dict({
        "name": "closed",
        "conditions": [{"field": "event.payload.nonexistent", "op": "eq", "value": 1}],
        "action": {"type": "create_alert"},
    })
    assert RuleEngine([rule]).evaluate(ppe_event()) == []


def test_unknown_op_fails_closed():
    rule = Rule.from_dict({
        "name": "badop",
        "conditions": [{"field": "event.zone", "op": "regex", "value": ".*"}],
        "action": {"type": "create_alert"},
    })
    assert RuleEngine([rule]).evaluate(ppe_event()) == []


def test_event_type_filter_and_disabled():
    rule = Rule.from_dict({
        "name": "falls-only", "event_types": ["fall"], "conditions": [],
        "action": {"type": "create_alert"},
    })
    engine = RuleEngine([rule])
    assert engine.evaluate(ppe_event()) == []          # wrong type
    rule.enabled = False
    fall = Event(worker_id="w-1", camera_id="c", event_type=EventType.FALL, payload={})
    assert engine.evaluate(fall) == []                 # disabled


def test_cooldown_per_rule_per_worker():
    clock = FakeClock()
    rule = Rule.from_dict({
        "name": "cd", "event_types": ["ppe"], "conditions": [],
        "action": {"type": "notify", "message": "x"}, "cooldown_s": 300,
    })
    engine = RuleEngine([rule], clock=clock)
    assert len(engine.evaluate(ppe_event(worker="w-1"))) == 1
    assert engine.evaluate(ppe_event(worker="w-1")) == []      # same worker → cooldown
    assert len(engine.evaluate(ppe_event(worker="w-2"))) == 1  # other worker fine
    clock.advance(301)
    assert len(engine.evaluate(ppe_event(worker="w-1"))) == 1  # cooldown expired


def test_message_templating():
    rule = Rule.from_dict({
        "name": "tpl", "event_types": ["ppe"], "conditions": [],
        "action": {"type": "create_alert",
                   "message": "worker {event.worker_id} in {event.zone} missing {event.payload.ppe_item}"},
    })
    engine = RuleEngine([rule])
    actions = engine.evaluate(ppe_event(worker="w-9", zone="zone-z", item="vest"))
    assert actions[0].params["message"] == "worker w-9 in zone-z missing vest"


def test_priority_ordering():
    low = Rule.from_dict({"name": "low", "priority": 10, "conditions": [],
                          "action": {"type": "notify"}})
    high = Rule.from_dict({"name": "high", "priority": 99, "conditions": [],
                           "action": {"type": "notify"}})
    engine = RuleEngine([low, high])
    assert [r.name for r in engine.list_rules()] == ["high", "low"]


# --------------------------------------------------------------------------- spec exemplars


def test_spec_rule_helmet_in_danger_zone_critical():
    engine = RuleEngine(default_rules())
    ev = ppe_event(item="helmet")
    # not in danger zone → no critical alert
    assert engine.evaluate(ev, context={"in_danger_zone": False}) == []
    actions = engine.evaluate(ev, context={"in_danger_zone": True})
    assert len(actions) == 1
    assert actions[0].action_type == "create_alert"
    assert actions[0].params["severity"] == "critical"


def test_spec_rule_crack_severity_triggers_immediate_inspection():
    engine = RuleEngine(default_rules())
    assert engine.evaluate(crack_event(0.5)) == []
    actions = engine.evaluate(crack_event(0.9))
    assert any(a.action_type == "request_inspection" and a.params["priority"] == "immediate"
               for a in actions)


def test_spec_rule_rebar_deviation_generates_rfi():
    engine = RuleEngine(default_rules())
    assert engine.evaluate(measurement_event(deviation_mm=15.0)) == []
    actions = engine.evaluate(measurement_event(deviation_mm=25.0))
    assert any(a.action_type == "generate_rfi" for a in actions)
    # wrong element type → no RFI
    assert engine.evaluate(measurement_event(element="pipe_diameter", deviation_mm=30.0)) == []
