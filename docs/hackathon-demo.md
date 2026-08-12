# Hackathon Demo Runbook

## Prepare once

```bash
make setup
make fetch-models
make llm-pull
make demo-check
```

The real-time safety loop uses YOLO pose and PPE weights. The worker assistant uses the local
`gemma4:e4b-it-qat` model. A missing structural-damage checkpoint affects inspection mode only;
do not claim crack classification unless team-trained weights are installed.

## Start the demo

```bash
make run-all
```

Open <http://localhost:3000> and sign in as `manager` / `manager123`. Connect the phone with both
ADB tunnels, install the prepared APK, then sign in as `worker1` / `worker123`:

```bash
adb reverse tcp:8100 tcp:8100
adb reverse tcp:8000 tcp:8000
adb install -r worker_app/build/app/outputs/flutter-apk/app-debug.apk
```

Use `http://127.0.0.1:8100` as the server address on the phone. To rebuild the APK after a mobile
code change, run `flutter build apk --debug` from `worker_app/`.

If `adb` is not on `PATH`, use `$ANDROID_HOME/platform-tools/adb`. Install an offline English
speech-recognition pack and a TTS voice on the phone before arriving. The beacon uses tap-to-arm
Android STT: say the wake phrase and command in one utterance. It is not an always-listening
background microphone.

## Three-minute judge flow

1. Show the manager's live worker feed and the phone streaming indicator.
2. On the phone, open **Pilot**, tap the amber beacon, and say: “Hey FieldPilot, identify this
   tool and tell me any visible safety concern.” Attach a clear photo if prompted.
3. Say: “Hey FieldPilot, measure this rebar spacing.” In the measurement tool, mark a known
   100 mm reference and then the target endpoints. Enter a spec to show the deterministic
   tolerance result.
4. Say: “Hey FieldPilot, report smoke near the stairs.” Show that FieldPilot prepares a critical
   report but requires the worker to confirm before notifying the manager.
5. Run `make demo-events` to populate the manager board with PPE, proximity, structural, and
   measurement examples when a live hazard is unsuitable for an indoor demo.

## Claims to make accurately

- Safety detection is always on and does not wait for Gemma.
- Gemma understands the worker's photo/question and selects tools; geometry computes dimensions.
- Alerts and assistant answers are spoken on the worker's phone.
- Model output is advisory. Critical decisions and hazard submissions remain deterministic or
  human-confirmed.
- Demo events are labelled examples, not live detector evidence.

## Improve accuracy with site footage

Do not fine-tune during the live pitch. Collect representative day/night footage first, obtain
consent, and blur bystanders. Managers can open `/dataset` to capture frames from a live phone,
correct draft PPE boxes, confirm negative images, and export session-safe YOLO labels. For the full
workflow, follow [model-training.md](model-training.md), audit the export, and run transfer learning:

```bash
make audit-ppe DATA=data/site_ppe/data.yaml
make train-ppe DATA=data/site_ppe/data.yaml EPOCHS=60
```

Supervisor corrections also feed the incremental `make train` learning service. Keep fall and
proximity logic separately validated: a PPE fine-tune is not proof of those hazards.
