"""FieldPilot CLI entrypoint.

Examples:
    python -m fieldpilot.run --source webcam
    python -m fieldpilot.run --source file --file data/videos/sample.mp4 --show
    python -m fieldpilot.run --validate 10min          # headless stability run, exits 0 on success
    python -m fieldpilot.run --bench                    # latency harness (detection→alert budget)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time

from fieldpilot.core.config import load_config
from fieldpilot.core.video_source import VideoSource
from fieldpilot.logging_.logger import get_logger, setup_logging

log = get_logger("fieldpilot")


def _parse_duration(text: str) -> float:
    """Parse '10min', '30s', '2m', '90' → seconds."""

    text = text.strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h)?", text)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration: {text!r}")
    value = float(m.group(1))
    unit = m.group(2) or "s"
    factor = {"ms": 0.001, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600}[unit]
    return value * factor


def _build_source(cfg, kind: str, file_path: str | None, pace: bool, max_seconds: float | None):
    v = cfg.section("video")
    return VideoSource(
        kind=kind,
        webcam_index=int(v.get("webcam_index", 0)),
        file_path=file_path or v.get("file_path"),
        target_fps=int(v.get("target_fps", 30)),
        queue_maxsize=int(v.get("queue_maxsize", 4)),
        pace=pace,
    )


async def _run_pipeline(cfg, args) -> int:
    from fieldpilot.core.pipeline import Pipeline  # deferred: pulls in torch/ultralytics

    if args.validate:
        max_seconds = _parse_duration(args.validate)
    elif args.duration:
        max_seconds = _parse_duration(args.duration)
    else:
        max_seconds = None
    kind = "synthetic" if (args.validate and args.source == "webcam" and not args.file) else args.source
    if args.validate and kind == "synthetic":
        log.info("validation run using synthetic source (no camera required)")
    # validation runs unpaced (as-fast-as-possible) to stress the loop; live runs pace to fps.
    pace = not args.validate
    source = _build_source(cfg, kind, args.file, pace=pace, max_seconds=max_seconds)
    pipeline = Pipeline(cfg, show=args.show)
    summary = await pipeline.run(source, max_seconds=max_seconds)

    log.info("run summary:\n%s", json.dumps(summary, indent=2))
    if args.validate:
        budget = summary.get("latency_ms_median")
        if budget is not None and budget >= 500:
            log.warning("median detection→alert latency %.0f ms exceeds 500 ms budget", budget)
    return 0


async def _run_bench(cfg) -> int:
    """Measure the components of the detection→alert path and estimate end-to-end latency."""

    from statistics import median

    from fieldpilot.alerts.dispatcher import AlertDispatcher
    from fieldpilot.core.types import Frame, HazardEvent, HazardType, Severity
    from fieldpilot.core.video_source import _synthetic_frame
    from fieldpilot.core.vision_engine import VisionEngine

    log.info("benchmarking — warming up model…")
    engine = VisionEngine(cfg)
    # warmup
    for i in range(5):
        engine.infer(Frame(index=i, ts_monotonic=time.monotonic(), image=_synthetic_frame(i)))

    infer_ms: list[float] = []
    for i in range(60):
        frame = Frame(index=i, ts_monotonic=time.monotonic(), image=_synthetic_frame(i))
        t0 = time.monotonic()
        engine.infer(frame)
        infer_ms.append((time.monotonic() - t0) * 1000.0)

    dispatcher = AlertDispatcher(cfg)
    dispatcher.dry_run = True  # measure admission latency without spamming audio/haptics
    dispatch_ms: list[float] = []
    for i in range(50):
        ev = HazardEvent(
            hazard_type=HazardType.FALL, severity=Severity.HIGH, message="bench",
            frame_index=i, ts_monotonic=time.monotonic(),
        )
        t0 = time.monotonic()
        dispatcher.dispatch(ev)
        dispatch_ms.append((time.monotonic() - t0) * 1000.0)
        # avoid cooldown suppression during measurement
        dispatcher._last.clear()
    dispatcher.shutdown()

    frame_interval = 1000.0 / max(1, int(cfg.get("video.target_fps", 30)))
    infer_med = median(infer_ms)
    dispatch_med = median(dispatch_ms)
    end_to_end = infer_med + dispatch_med + frame_interval  # + one frame of capture jitter

    report = {
        "infer_ms_median": round(infer_med, 2),
        "dispatch_ms_median": round(dispatch_med, 2),
        "capture_jitter_ms": round(frame_interval, 2),
        "end_to_end_estimate_ms": round(end_to_end, 2),
        "budget_ms": 500,
        "within_budget": end_to_end < 500,
    }
    log.info("latency benchmark:\n%s", json.dumps(report, indent=2))
    return 0 if report["within_budget"] else 1


def _run_demo_alert(cfg) -> int:
    """Fire one sample alert per hazard category so the output channels can be heard/felt."""

    import time as _time

    from fieldpilot.alerts.dispatcher import AlertDispatcher
    from fieldpilot.core.types import HazardEvent, HazardType, Severity

    dispatcher = AlertDispatcher(cfg)
    demos = [
        (HazardType.FALL, Severity.HIGH, "Possible fall detected for worker 3."),
        (HazardType.PPE_MISSING, Severity.MEDIUM, "Worker 3 appears to be missing a hard hat."),
        (HazardType.UNNOTICED_HAZARD, Severity.MEDIUM, "Worker 3 has not acknowledged a fall hazard."),
        (HazardType.PROXIMITY, Severity.LOW, "Worker 3 is close to a moving hazard."),
    ]
    log.info("playing %d demo alerts — you should hear an earcon then a spoken alert for each",
             len(demos))
    for hazard_type, severity, message in demos:
        ev = HazardEvent(
            hazard_type=hazard_type, severity=severity, message=message,
            frame_index=0, ts_monotonic=time.monotonic(),
        )
        dispatcher.dispatch(ev)
        dispatcher._last.clear()  # bypass cooldown for the demo
        _time.sleep(3.5)          # let the earcon + speech finish before the next
    _time.sleep(1.0)
    dispatcher.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fieldpilot", description="FieldPilot AI edge safety loop")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--source", choices=["webcam", "file", "synthetic"], default="webcam")
    p.add_argument("--file", default=None, help="video file path (with --source file)")
    p.add_argument("--show", action="store_true", help="show an annotated preview window")
    p.add_argument("--validate", metavar="DURATION", default=None,
                   help="synthetic headless stress run for a fixed duration then exit (e.g. 10min)")
    p.add_argument("--duration", metavar="DURATION", default=None,
                   help="time-box a live run (webcam/file) then print a summary (e.g. 60s)")
    p.add_argument("--bench", action="store_true", help="run the latency benchmark and exit")
    p.add_argument("--demo-alert", action="store_true",
                   help="play one sample alert per hazard category (test audio/haptics)")
    p.add_argument("--gui", action="store_true",
                   help="launch the web GUI (live annotated feed + analysis dashboard)")
    p.add_argument("--host", default="0.0.0.0", help="GUI bind host")
    p.add_argument("--port", type=int, default=8000, help="GUI port")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.get("logging.level", "INFO"))
    try:
        if args.gui:
            from fieldpilot.display.server import run_gui

            return run_gui(config_path=args.config, source_kind=args.source,
                           file_path=args.file, host=args.host, port=args.port)
        if args.demo_alert:
            return _run_demo_alert(cfg)
        if args.bench:
            return asyncio.run(_run_bench(cfg))
        return asyncio.run(_run_pipeline(cfg, args))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
