"""Inspection AI — structural defect detection (cracks / rotations / surface damage).

Uses the fine-tuned YOLO model (`models/structural_damage_best.pt`, classes:
Minorrotation / Moderaterotation / Severerotation). Runs only when *inspection mode*
is ON — toggled at runtime from the dashboard via the `control.inspection` bus channel.
Detections become `crack` events on the bus; the rules engine escalates severe ones
(severity_score > 0.85) into immediate inspection requests.
"""

from fieldpilot.inspection.detector import InspectionDetector

__all__ = ["InspectionDetector"]
