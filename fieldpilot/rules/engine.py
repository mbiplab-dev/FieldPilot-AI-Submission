"""Rules Engine.

Inputs:  events from the bus (already deduplicated by the trigger engine) plus optional
         context (e.g. "worker currently in a danger zone").
Output:  zero or more actions — the ONLY place in the system allowed to request a
         notification, an RFI, or an inspection.

Rules are configurable and stored in the database. A rule:

    {
      "name": "no-helmet-danger-zone",
      "enabled": true,
      "priority": 100,
      "event_types": ["ppe"],
      "conditions": [
        {"field": "event.payload.ppe_item", "op": "eq", "value": "helmet"},
        {"field": "context.in_danger_zone", "op": "truthy"}
      ],
      "action": {"type": "create_alert", "severity": "critical",
                 "message": "Worker {event.worker_id} without helmet in danger zone"},
      "cooldown_s": 300
    }

All conditions are AND-ed. Field paths resolve over {"event": ..., "context": ...}.
Supported ops: eq, ne, gt, gte, lt, lte, in, not_in, contains, exists, truthy.
"""

from __future__ import annotations

import operator
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fieldpilot.events.schema import Event
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.rules.engine")

_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": lambda a, b: a in (b or []),
    "not_in": lambda a, b: a not in (b or []),
    "contains": lambda a, b: b in (a or "") if isinstance(a, (str, list)) else False,
    "exists": lambda a, _b: a is not None,
    "truthy": lambda a, _b: bool(a),
}

_MISSING = object()


@dataclass
class Rule:
    rule_id: str
    name: str
    enabled: bool = True
    priority: int = 100
    event_types: list[str] = field(default_factory=list)  # empty = all types
    conditions: list[dict[str, Any]] = field(default_factory=list)
    action: dict[str, Any] = field(default_factory=dict)
    cooldown_s: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "event_types": list(self.event_types),
            "conditions": list(self.conditions),
            "action": dict(self.action),
            "cooldown_s": self.cooldown_s,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Rule:
        return cls(
            rule_id=d.get("rule_id") or uuid.uuid4().hex,
            name=d["name"],
            enabled=bool(d.get("enabled", True)),
            priority=int(d.get("priority", 100)),
            event_types=list(d.get("event_types") or []),
            conditions=list(d.get("conditions") or []),
            action=dict(d.get("action") or {}),
            cooldown_s=float(d.get("cooldown_s", 0.0)),
            created_at=float(d.get("created_at", time.time())),
        )


@dataclass
class RuleAction:
    """One fired action: type + rendered params + the rule that produced it."""

    action_type: str          # create_alert | generate_rfi | request_inspection | notify
    rule_id: str
    rule_name: str
    params: dict[str, Any]
    event_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "params": self.params,
            "event_id": self.event_id,
        }


def _resolve(path: str, scope: dict[str, Any]) -> Any:
    node: Any = scope
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return _MISSING
    return node


class _PathFormatter(string.Formatter):
    """`{event.payload.ppe_item}` resolves through nested dicts; unknown paths stay literal."""

    def get_field(self, field_name, args, kwargs):
        node: Any = kwargs
        for part in field_name.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return "{" + field_name + "}", field_name
        return node, field_name


_FORMATTER = _PathFormatter()


class RuleEngine:
    def __init__(self, rules: list[Rule] | None = None, clock=time.time) -> None:
        self._rules: dict[str, Rule] = {}
        self._last_fired: dict[tuple[str, str], float] = {}
        self.clock = clock
        for rule in rules or []:
            self.add_rule(rule)

    # -- rule management (backed by the platform store in the service layer) ---

    def add_rule(self, rule: Rule) -> None:
        self._rules[rule.rule_id] = rule

    def remove_rule(self, rule_id: str) -> bool:
        return self._rules.pop(rule_id, None) is not None

    def get_rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def list_rules(self) -> list[Rule]:
        return sorted(self._rules.values(), key=lambda r: -r.priority)

    def replace_rules(self, rules: list[Rule]) -> None:
        self._rules = {r.rule_id: r for r in rules}

    # -- evaluation ------------------------------------------------------------

    def evaluate(self, event: Event, context: dict[str, Any] | None = None) -> list[RuleAction]:
        scope = {"event": event.model_dump(mode="json"), "context": context or {}}
        fired: list[RuleAction] = []
        for rule in self.list_rules():
            if not rule.enabled:
                continue
            if rule.event_types and event.event_type.value not in rule.event_types:
                continue
            if self._in_cooldown(rule, event):
                continue
            if not all(self._check(cond, scope) for cond in rule.conditions):
                continue
            action_type = str(rule.action.get("type", "create_alert"))
            params = {
                k: self._render(v, scope) for k, v in rule.action.items() if k != "type"
            }
            fired.append(
                RuleAction(
                    action_type=action_type,
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    params=params,
                    event_id=event.event_id,
                )
            )
            self._mark_fired(rule, event)
        return fired

    def _check(self, cond: dict[str, Any], scope: dict[str, Any]) -> bool:
        field_path = str(cond.get("field", ""))
        op_name = str(cond.get("op", "eq"))
        expected = cond.get("value")
        op = _OPS.get(op_name)
        if op is None:
            log.warning("unknown rule op %r — condition fails closed", op_name)
            return False
        actual = _resolve(field_path, scope)
        if actual is _MISSING:
            # field absent: only an explicit "exists" check could pass — and it can't.
            return False
        try:
            return bool(op(actual, expected))
        except TypeError:
            return False

    def _render(self, value: Any, scope: dict[str, Any]) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return _FORMATTER.vformat(value, (), scope)
        except (KeyError, ValueError, IndexError):
            return value

    # -- cooldowns (per rule + per subject so one noisy worker can't starve others)

    def _cooldown_key(self, rule: Rule, event: Event) -> tuple[str, str]:
        return (rule.rule_id, event.worker_id or event.camera_id)

    def _in_cooldown(self, rule: Rule, event: Event) -> bool:
        if rule.cooldown_s <= 0:
            return False
        last = self._last_fired.get(self._cooldown_key(rule, event))
        return last is not None and (self.clock() - last) < rule.cooldown_s

    def _mark_fired(self, rule: Rule, event: Event) -> None:
        self._last_fired[self._cooldown_key(rule, event)] = self.clock()


# ---------------------------------------------------------------- default seed rules


def default_rules() -> list[Rule]:
    """The three spec exemplars, seeded on first boot."""

    return [
        Rule.from_dict({
            "name": "no-helmet-in-danger-zone",
            "priority": 100,
            "event_types": ["ppe"],
            "conditions": [
                {"field": "event.payload.ppe_item", "op": "eq", "value": "helmet"},
                {"field": "context.in_danger_zone", "op": "truthy"},
            ],
            "action": {
                "type": "create_alert",
                "severity": "critical",
                "message": "CRITICAL: worker {event.worker_id} without helmet in danger zone {event.zone}",
            },
            "cooldown_s": 300,
        }),
        Rule.from_dict({
            "name": "severe-crack-immediate-inspection",
            "priority": 90,
            "event_types": ["crack"],
            "conditions": [
                {"field": "event.payload.severity_score", "op": "gt", "value": 0.85},
            ],
            "action": {
                "type": "request_inspection",
                "priority": "immediate",
                "message": "Crack severity {event.payload.severity_score} in zone {event.zone} — immediate inspection",
            },
            "cooldown_s": 600,
        }),
        Rule.from_dict({
            "name": "rebar-deviation-rfi",
            "priority": 80,
            "event_types": ["measurement"],
            "conditions": [
                {"field": "event.payload.element", "op": "eq", "value": "rebar_spacing"},
                {"field": "event.payload.deviation_mm", "op": "gt", "value": 20},
            ],
            "action": {
                "type": "generate_rfi",
                "priority": "high",
                "message": "Rebar spacing deviation {event.payload.deviation_mm}mm exceeds 20mm tolerance in {event.zone}",
            },
            "cooldown_s": 1800,
        }),
    ]
