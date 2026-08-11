"""Spatial calibration & measurement (Milestone 2 / docs/possibilities.md "measure distances").

Uses a known physical reference object (e.g. a hard-hat sticker, a standard card, a survey target)
to establish a pixels-per-millimetre ratio, then converts pixel distances to real-world millimetres
and checks them against a spec with a tolerance (e.g. rebar spacing). The reference is found via
contour detection + a minimum-area bounding rectangle, per the PRD.

Pure OpenCV/numpy — unit-testable against a synthetic frame with a known reference size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Reference:
    center: tuple[float, float]
    size_px: tuple[float, float]   # (width, height) of the min-area rect, pixels
    angle: float
    long_side_px: float


@dataclass
class SpecCheck:
    measured_mm: float
    spec_mm: float
    deviation_mm: float
    within_tolerance: bool


class MeasurementCalibrator:
    def __init__(self, cfg=None, reference_mm: float | None = None):
        if cfg is not None:
            self.reference_mm = float(cfg.get("compliance.reference_object_mm", 88.9))
            self.min_area = float(cfg.get("compliance.min_contour_area", 1000))
            self.tol_mm = float(cfg.get("compliance.deviation_tolerance_mm", 5.0))
        else:
            self.reference_mm = 88.9
            self.min_area = 1000.0
            self.tol_mm = 5.0
        if reference_mm is not None:
            self.reference_mm = float(reference_mm)
        self.px_per_mm: float | None = None

    def find_reference(self, image_bgr: np.ndarray) -> Reference | None:
        """Largest bright rectangular blob → its min-area bounding rectangle."""

        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = self.min_area
        for c in contours:
            area = cv2.contourArea(c)
            if area < best_area:
                continue
            best_area = area
            best = c
        if best is None:
            return None
        (cx, cy), (w, h), angle = cv2.minAreaRect(best)
        return Reference((cx, cy), (w, h), angle, max(w, h))

    def calibrate(self, image_bgr: np.ndarray) -> float | None:
        """Set px_per_mm from the reference object's long side. Returns the ratio or None."""

        ref = self.find_reference(image_bgr)
        if ref is None or ref.long_side_px <= 0:
            return None
        self.px_per_mm = ref.long_side_px / self.reference_mm
        return self.px_per_mm

    def mm_between(self, p1, p2) -> float:
        if not self.px_per_mm:
            raise RuntimeError("calibrate() must succeed before measuring")
        dist_px = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        return dist_px / self.px_per_mm

    def check_spec(self, p1, p2, spec_mm: float, tol_mm: float | None = None) -> SpecCheck:
        measured = self.mm_between(p1, p2)
        tol = self.tol_mm if tol_mm is None else tol_mm
        deviation = measured - spec_mm
        return SpecCheck(
            measured_mm=round(measured, 2),
            spec_mm=spec_mm,
            deviation_mm=round(deviation, 2),
            within_tolerance=abs(deviation) <= tol,
        )
