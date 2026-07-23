# possibilities.md → FieldPilot coverage

`possibilities.md` is the viso.ai catalogue of computer-vision applications in construction. This
maps each to what FieldPilot actually does today, honestly. Legend: ✅ done · ◑ partial · 🔜 next
(planned milestone) · ⛔ needs a new model/dataset or hardware we don't have yet.

| # | Application (possibilities.md) | Status | Where / note |
|---|---|---|---|
| 1 | Equipment & vehicle detection + tracking | ◑ | `ppe.py` surfaces `machinery`/`vehicle` boxes; drawn orange. Persistent IDs on equipment not yet (only workers tracked). |
| 2 | PPE detection (hard hat, hi-vis vest) | ✅ | `safety/ppe.py` — Hardhat/Vest + explicit NO-Hardhat/NO-Vest violations → alerts. |
| 3 | Safety & security monitoring (worker near machinery) | ✅ | `safety/proximity.py` — danger-zone proximity → HIGH alert. |
| 4 | Intelligent workforce mgmt (tracking, counting) | ◑ | Live worker count + unique-track count in HUD/stats. Heatmaps/shift analytics future. |
| 5 | Ergonomic risk assessment (pose) | ◑ | Full 17-pt pose live; sustained-posture ergonomic scoring is a small future add. |
| 6 | Process tracking — safety-protocol-break detection | ◑ | Breaks = falls / PPE / proximity (done). Schedule/progress tracking future. |
| 7 | Measure distances & volume | ✅ (dist) | `compliance/calibration.py` — px→mm + spec-deviation. `--measure IMAGE`. Volume needs depth. |
| 8 | Automatic construction zone detection | ◑ | Zones modelled in config (for broadcast); auto-detecting zones from imagery is future. |
| 9 | Automated inspection | ◑ | PPE inspection done; general asset/vehicle inspection future. |
| 10 | Dangerous-goods sign recognition | ⛔ | Feasible: plug a sign detector into the same `ppe_model` slot pattern. |
| 11 | Construction vehicle ID (ANPR / plates) | ⛔ | Feasible: add a plate detector + OCR stage. |
| 12 | Automated quality control (material defects) | ⛔ | Needs a defect/anomaly model + dataset. |
| 13 | Structural defect detection (cracks, corrosion) | ⛔ | Needs a crack/corrosion segmentation model. |
| 14 | Material management (classify, quantify) | ⛔ | Needs a materials classifier. |
| 15 | Read analog dials / gauges | ⛔ | Needs a gauge model + reading head. |
| 16 | Asset management & maintenance | ⛔ | Needs an asset registry + condition model. |
| 17 | 3D scanning / point clouds | ⛔ | Needs depth/stereo/LiDAR hardware. |
| 18 | Outdoor / indoor mapping (SLAM) | ⛔ | Needs SLAM + odometry; out of scope for a single webcam. |

## How the ⛔ items get added

Every ⛔ that is a **2-D object/defect detector** (signs, plates, cracks, materials, gauges, assets)
drops into the existing pluggable pattern: train or obtain YOLO weights, point a config key at them,
add a small consumer in `safety/` or `compliance/`, and draw it in `annotate()`. The ⛔ items needing
**depth or SLAM** (volume, 3-D scanning, mapping) require sensors beyond the current camera and are
genuinely out of scope until that hardware exists.

## Built this iteration

- Upgraded pose backbone **yolov8n → yolo11m** (18 ms; n/s/m/l selectable).
- Real **PPE detection** (10-class construction model) with compliance + violation alerts.
- **Proximity / danger-zone** monitoring (worker ↔ machinery/vehicle).
- **Equipment/vehicle** detection overlay.
- Live **fall-risk meter** per worker + tuned fall thresholds.
- **Measurement/calibration** (px→mm, spec-deviation) — Milestone 2 begins.
