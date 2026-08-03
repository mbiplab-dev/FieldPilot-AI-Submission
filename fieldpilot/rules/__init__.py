"""Rules Engine — converts filtered events into actionable outcomes.

Models emit events; the trigger engine filters them; the RULES ENGINE decides what each
event means for the site: escalate an alert, request an inspection, generate an RFI,
notify a channel. Rules are data (stored in the database, editable via REST), not code.
"""

from fieldpilot.rules.engine import Rule, RuleAction, RuleEngine

__all__ = ["Rule", "RuleAction", "RuleEngine"]
