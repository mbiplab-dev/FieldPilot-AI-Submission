"""Measurement calibration: known reference → correct px/mm → correct mm distance + spec check."""

from __future__ import annotations

import numpy as np

from fieldpilot.compliance.calibration import MeasurementCalibrator


def _frame_with_rect(w_px=200, h_px=100):
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    x0, y0 = 220, 190
    img[y0:y0 + h_px, x0:x0 + w_px] = (255, 255, 255)  # white reference rectangle
    return img


def test_calibrate_and_measure():
    # 200px long side represents a 100mm reference → 2 px/mm.
    cal = MeasurementCalibrator(reference_mm=100.0)
    ratio = cal.calibrate(_frame_with_rect(200, 100))
    assert ratio is not None
    assert abs(ratio - 2.0) < 0.05

    # a 50px segment → 25mm.
    mm = cal.mm_between((100, 100), (150, 100))
    assert abs(mm - 25.0) < 1.0


def test_spec_check_flags_deviation():
    cal = MeasurementCalibrator(reference_mm=100.0)
    cal.calibrate(_frame_with_rect(200, 100))
    # measured 100mm (200px) vs 90mm spec, 5mm tol → 10mm deviation, out of tolerance.
    check = cal.check_spec((0, 0), (200, 0), spec_mm=90.0, tol_mm=5.0)
    assert abs(check.measured_mm - 100.0) < 1.0
    assert not check.within_tolerance
    assert check.deviation_mm > 0


def test_no_reference_returns_none():
    cal = MeasurementCalibrator(reference_mm=100.0)
    assert cal.calibrate(np.zeros((480, 640, 3), dtype=np.uint8)) is None
