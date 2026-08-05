"""Turn an alert into the sentence a person actually hears.

The product's flagship promise is that the worker *hears* the verdict — "Stop work. Rebar spacing
is 40 millimetres above spec." — through the glasses' open-ear speaker, while the supervisor hears
a third-person version of the same fact on the dashboard.

That makes the spoken phrase **data the system owes its clients**, not a side effect of a
server-side `espeak` call. `alerts/tts.py` renders audio on the *server's* speakers, which is the
wrong machine: the people who need to hear an alert are holding a phone or looking at a browser.
So the phrase travels on the broadcast frame and each client synthesises it locally. That also
means speech keeps working on a disconnected site and needs no API key or system binary — which
matters, because a host with no espeak-ng installed cannot speak at all through `tts.py`.

Three audiences, because the same fact is a different sentence depending on who hears it:

  ``worker``     the person the alert is about — imperative and second-person, no ids to read out
                 ("Stop work. Put on your hard hat.")
  ``peer``       another worker in the same zone — third-person, lower urgency, keeps the advisory
                 framing that PRD §4.4 requires ("Heads up. A worker is too close to the excavator
                 in your zone.")
  ``dashboard``  the supervisor — third-person and fully qualified, since they are triaging many
                 workers across many zones ("High alert. Worker w-9 is missing a hard hat in
                 zone A12.")

Every function here is pure and total: a partial or malformed alert dict degrades the sentence, it
never raises. Losing the alert because we could not phrase it would be the worst possible failure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: The three delivery paths a phrase can be written for.
AUDIENCES = ("worker", "peer", "dashboard")

#: How urgently the sentence opens, per severity. Critical and high both stop the job.
_LEAD_IN = {
    "critical": {"worker": "Stop work.", "peer": "Warning.", "dashboard": "Critical alert."},
    "high": {"worker": "Stop work.", "peer": "Heads up.", "dashboard": "High alert."},
    "medium": {"worker": "Heads up.", "peer": "Heads up.", "dashboard": "Medium alert."},
    "low": {"worker": "Note.", "peer": "Note.", "dashboard": "Low alert."},
}

#: Spoken forms of the PPE items `safety/ppe.py` tracks. "hard hat" reads better than "helmet".
_PPE_PHRASE = {
    "helmet": "hard hat",
    "hard_hat": "hard hat",
    "hardhat": "hard hat",
    "vest": "high visibility vest",
    "safety_vest": "high visibility vest",
    "gloves": "gloves",
    "goggles": "safety goggles",
    "boots": "safety boots",
    "harness": "safety harness",
    "mask": "mask",
}

#: Units expanded so a synthesiser says "millimetres" instead of spelling out "em em".
_UNIT_WORDS = {
    "mm": "millimetres",
    "cm": "centimetres",
    "kg": "kilograms",
    "m": "metres",
}

# Longest-first so "40mm" expands to millimetres rather than matching the bare "m".
_UNIT_RE = re.compile(r"(?<=\d)\s*(mm|cm|kg|m)\b")


def spoken_phrase(alert: Mapping[str, Any], *, audience: str = "worker") -> str:
    """The sentence `audience` should hear for `alert`.

    Never raises: an alert with no recognisable content still yields a usable sentence.
    """

    if audience not in AUDIENCES:
        audience = "worker"
    severity = _severity(alert)
    lead = _LEAD_IN.get(severity, _LEAD_IN["medium"])[audience]
    body = _body(alert, audience)
    return _speakable(f"{lead} {body}")


def _severity(alert: Mapping[str, Any]) -> str:
    value = str(alert.get("severity") or "medium").strip().lower()
    return value if value in _LEAD_IN else "medium"


def _body(alert: Mapping[str, Any], audience: str) -> str:
    """The factual clause, without the severity lead-in or trailing punctuation."""

    payload = alert.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    event_type = str(alert.get("event_type") or "").strip().lower()

    builder = _BODIES.get(_family(event_type))
    clause = builder(alert, payload, audience) if builder else None
    if not clause:
        # Fall back to the detector's own message. It is written for a screen, but a degraded
        # sentence beats silence when a new detector ships an event_type this module has not
        # learned yet.
        clause = str(alert.get("message") or "").strip()
    if not clause:
        clause = f"{event_type.replace('_', ' ') or 'A hazard'} was detected"

    # The clause may be a detector message that already ends in a period. Appending the location
    # to that would strand it as its own fragment ("Something odd. In your zone.").
    return f"{clause.rstrip('.!? ')}{_where(alert, audience)}"


def _family(event_type: str) -> str:
    """Collapse detector-specific names onto the event family speech is written for.

    Detectors emit both bare families (`fall`) and qualified names (`fall_detected`,
    `ppe_missing`), and `unnoticed_hazard` is the attention detector's own type. Matching on a
    prefix keeps this table small instead of enumerating every variant.
    """

    for family in ("measurement", "ppe", "fall", "proximity", "crack", "inspection", "fire", "gas"):
        if event_type.startswith(family):
            return family
    if "hazard" in event_type:
        return "hazard"
    return event_type


def _where(alert: Mapping[str, Any], audience: str) -> str:
    """Location suffix. A worker is already standing in the zone, so we do not read it back."""

    zone = str(alert.get("zone") or "").strip()
    if not zone:
        return ""
    if audience == "dashboard":
        return f" in {zone}"
    if audience == "peer":
        return " in your zone"
    return ""


# --------------------------------------------------------------------------- per-family clauses


def _measurement_body(alert, payload, audience) -> str:
    """The deck's headline example: "rebar spacing is 40 millimetres above spec"."""

    element = str(payload.get("element") or "measurement").replace("_", " ")
    deviation = _float(payload.get("deviation_mm"))
    if deviation is None:
        measured, spec = _float(payload.get("measured_mm")), _float(payload.get("spec_mm"))
        deviation = None if measured is None or spec is None else measured - spec
    if deviation is None:
        return f"{element} is outside spec"
    direction = "above" if deviation >= 0 else "below"
    return f"{element} is {_num(abs(deviation))} mm {direction} spec"


def _ppe_body(alert, payload, audience) -> str:
    raw = str(payload.get("ppe_item") or payload.get("item") or "").strip().lower()
    item = _PPE_PHRASE.get(raw, raw.replace("_", " "))
    if not item:
        return "required protective equipment is missing"
    if audience == "worker":
        return f"put on your {item}"
    return f"{_who(alert, audience)} is missing {_article(item)}"


def _fall_body(alert, payload, audience) -> str:
    if audience == "worker":
        return "a fall was detected. Stay down if you are hurt and signal for help"
    return f"a possible fall was detected for {_who(alert, audience)}"


def _proximity_body(alert, payload, audience) -> str:
    equipment = str(payload.get("equipment") or payload.get("kind") or "").replace("_", " ").strip()
    target = f" to the {equipment}" if equipment else " to heavy equipment"
    if audience == "worker":
        return f"you are dangerously close{target}. Step back"
    return f"{_who(alert, audience)} is dangerously close{target}"


def _defect_body(alert, payload, audience) -> str:
    defect = str(payload.get("defect") or payload.get("name") or "").replace("_", " ").strip()
    named = f": {defect}" if defect else ""
    if audience == "worker":
        return f"a structural defect was detected{named}. Do not proceed"
    return f"a structural defect was detected{named}"


def _fire_body(alert, payload, audience) -> str:
    if audience == "worker":
        return "fire detected. Evacuate now"
    return "fire was detected"


def _gas_body(alert, payload, audience) -> str:
    if audience == "worker":
        return "gas detected. Leave the area now"
    return "gas was detected"


def _hazard_body(alert, payload, audience) -> str:
    hazard = str(payload.get("hazard") or payload.get("category") or "").replace("_", " ").strip()
    named = hazard or "a hazard"
    if audience == "worker":
        return f"you have not noticed {named} nearby. Look up"
    return f"{_who(alert, audience)} has not noticed {named} nearby"


_BODIES = {
    "measurement": _measurement_body,
    "ppe": _ppe_body,
    "fall": _fall_body,
    "proximity": _proximity_body,
    "crack": _defect_body,
    "inspection": _defect_body,
    "fire": _fire_body,
    "gas": _gas_body,
    "hazard": _hazard_body,
}


# --------------------------------------------------------------------------- helpers


def _who(alert: Mapping[str, Any], audience: str) -> str:
    """How to refer to the alert's subject.

    A peer does not need — and under PRD §4.4 should not get — a colleague's id read aloud; the
    supervisor triaging many workers does.
    """

    worker = str(alert.get("worker_id") or "").strip()
    if audience == "dashboard" and worker:
        return f"worker {worker}"
    return "a worker"


#: PPE that is grammatically plural — "missing gloves", never "missing a gloves".
_PLURAL_PPE = frozenset({"gloves", "safety goggles", "safety boots"})


def _article(noun: str) -> str:
    if noun in _PLURAL_PPE:
        return noun
    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun}"


def _float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _num(value: float) -> str:
    """Trim a float to something worth reading aloud: 40.0 -> "40", 27.55 -> "27.6"."""

    rounded = round(value, 1)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"


def _speakable(text: str) -> str:
    """Expand units, tidy whitespace, and guarantee exactly one terminating period."""

    text = _UNIT_RE.sub(lambda m: " " + _UNIT_WORDS[m.group(1)], text)
    text = re.sub(r"\s+", " ", text.replace("_", " ")).strip()
    # Capitalise the clause after the lead-in's period so it reads as a sentence, and drop any
    # duplicated terminator the detector message may have brought with it.
    text = re.sub(r"([.!?])\s*([a-z])", lambda m: f"{m.group(1)} {m.group(2).upper()}", text)
    text = re.sub(r"[.!?]+$", "", text)
    return f"{text}." if text else ""
