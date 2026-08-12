# Safety Model Training

## What is pretrained

`models/yolo11m-pose.pt` is Ultralytics YOLO11-medium pose, pretrained on COCO's 17-person-keypoint
schema. FieldPilot derives fall posture, motion, and attention from those keypoints; it is not a
pretrained fall classifier. `models/ppe_css.pt` is an MIT-licensed YOLOv8n PPE fine-tune from
Hansung-Cho. Its model card reports precision 0.831, recall 0.685, mAP50 0.744, and mAP50–95 0.436
on its own validation set. Those numbers do not measure this site. The structural-damage checkpoint
is absent and inspection remains disabled.

## Build a site dataset

Record consented footage from the actual phone position across indoor/outdoor zones, shifts,
weather, distance, occlusion, and the PPE used on site. Extract frames at intervals to avoid nearly
identical samples. Label the existing ten classes with boxes:

`Hardhat`, `Mask`, `NO-Hardhat`, `NO-Mask`, `NO-Safety Vest`, `Person`, `Safety Cone`,
`Safety Vest`, `machinery`, `vehicle`.

Split by recording session—not random neighboring frames—so the same worker/background cannot
leak into train and validation. Keep a final site test set completely untouched. Aim for hundreds
of varied examples per safety-critical class; class balance matters more than recording hours.
Use empty `.txt` label files for confirmed negative images.

### Capture and review from the dashboard

Start the stack with `make run-all`, sign in as a manager, and open `/dataset`. Create separate
`train`, `val`, and optional `test` sessions for each recording condition. Select a live worker
feed, capture representative frames, then correct the detector's draft boxes in the review bench.
Mark real background frames as **Confirmed empty**; only reviewed frames are exported.

Use **Export reviewed dataset** after both train and validation have reviewed frames. The dashboard
shows the generated `data.yaml` path and ready-to-copy audit and training commands. Captures and
exports live under `data/training_captures/` and `data/site_datasets/`; both are ignored by Git.
Treat them as worker media: obtain consent, limit retention, and copy them only to approved storage.

For a reproducible public-data starting point, run:

```bash
make fetch-ppe-data
make prepare-ppe-data
make audit-ppe DATA=data/training/ppe_combined/data.yaml
```

The first command downloads the CC BY 4.0 Construction Site Safety dataset through Kaggle and the
AGPL-3.0 Ultralytics Construction-PPE archive. Archives, extracted images, merged data, and weights
remain ignored by Git. The preparation step maps only semantically equivalent labels into the
runtime's ten-class schema, removes exact duplicates, and keeps all Roboflow augmentations derived
from one source frame in the same split. `data/training/ppe_combined/MANIFEST.json` records source
URLs, licenses, mappings, counts, and leakage corrections.

This public validation set is suitable for an engineering comparison, not a site-acceptance claim:
the base checkpoint was trained on the older Construction Site Safety corpus. Replace or supplement
it with untouched phone footage split by recording session before calling a model site-ready.

The trainer enforces a minimum of 100 train images, 50 validation images, 20 train boxes and 10
validation boxes per runtime class. These are pipeline floors, not a claim that the resulting model
is deployment-ready; continue collecting hard examples after every staged test.

## Audit, train, and promote

Create a standard Ultralytics `data.yaml`, then run:

```bash
make audit-ppe DATA=data/site_ppe/data.yaml
make train-ppe DATA=data/site_ppe/data.yaml EPOCHS=60
```

The trainer defaults to one data-loader worker to fit the 16 GB hackathon laptop without filling
swap. On a machine with more free RAM, override it with `WORKERS=2`; use `BATCH=4` when you want a
fixed, conservative batch size instead of Ultralytics automatic sizing.

Training starts from `ppe_css.pt`, uses automatic mixed precision and GPU batch sizing, evaluates
the pretrained baseline and candidate on the same validation split, and writes every artifact to
`models/finetuned/`. A candidate is promoted to `promoted_ppe.pt` only when mAP50 and mAP50–95 do
not regress and recall remains within the configured tolerance. Point `detection.ppe_model` at the
promoted checkpoint only after staged acceptance tests with hard hats on/off, partial occlusion,
backlighting, multiple workers, and moving equipment.

## Train each task separately

Do not fine-tune the pose model with PPE boxes. Pose adaptation needs 17-keypoint annotations and
must be evaluated with pose OKS/mAP plus staged fall scenarios. Crack detection needs engineer-
reviewed defect boxes and a separate checkpoint. Gemma can explain a photo, but its answer is not
detector training data and must never be used as ground truth automatically.
