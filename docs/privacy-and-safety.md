# Privacy & Safety Posture

These are governance requirements, not optional features. They exist because the original PRD
overpromised ("zero-hallucination", guaranteed accuracy gains) in ways that create real liability for
a system that watches workers and speaks about their safety.

## Advisory, not authoritative

FieldPilot is a **decision-support aid**. It must never be presented or relied upon as the sole means
of hazard detection or as a safety-critical control. Site safety procedures remain the authority.
`config.yaml` records this as `app.posture: advisory`.

## Fail-safe behavior

- If the model, camera, or stream fails, the system must **degrade loudly** (log + operator-visible),
  never fail silently. A dead detector that emits no alerts must not be mistaken for "all clear".
- Alert side-effects are isolated on a worker pool so a failure in audio/TTS/haptics cannot take down
  detection, and vice-versa.

## False negatives are the dangerous class

A missed hazard (false negative) is far more consequential than a false alarm. Detector thresholds in
`config.yaml` should be tuned to favor recall for high-severity hazards (falls), accepting more false
positives. Every alert is logged for audit; suppression only ever happens via explicit, recorded
cooldowns.

## Privacy & consent (video of workers)

Continuous video capture of workers raises consent, labor, and data-protection obligations:

- Workers on site must be **informed and consent** to camera monitoring per local law and any union
  agreements.
- **Minimize retention.** The event store keeps structured events (bbox, keypoints-derived metrics),
  not raw video, by default. Raw frames are only persisted when explicitly enabled for the learning
  loop, and that store must have a retention limit and access controls (Milestone 3).
- **No biometric identification.** Worker "IDs" are ephemeral tracker IDs for continuity within a
  session, not identity. They must not be linked to personal identity without separate consent.

## Learning loop: measure, don't assume

The fine-tuning loop (Milestone 3) is **measure-and-gate**: it evaluates candidate weights on a
locked, immutable validation set and only promotes them if `mAP50` does not regress
(`learning.promote_if_delta_gte`). A regression is recorded and the old weights are kept. We do not
claim, and do not require, that human feedback always improves the model.
