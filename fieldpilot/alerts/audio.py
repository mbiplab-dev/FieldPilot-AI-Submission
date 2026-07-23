"""Minimal non-blocking WAV playback.

Uses whatever system player is present (ffplay/aplay/paplay) via a fire-and-forget subprocess, so
audio never blocks the inference loop and we avoid a hard PortAudio dependency. If no player is
available, playback is a no-op (the event is still logged and spoken via TTS where possible).
"""

from __future__ import annotations

import shutil
import subprocess

from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.audio")


def _pick_player() -> list[str] | None:
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
    if shutil.which("paplay"):
        return ["paplay"]
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    return None


_PLAYER = _pick_player()
if _PLAYER is None:
    log.warning("no audio player found (ffplay/paplay/aplay); earcons will be silent")


def play_wav_async(path: str) -> None:
    """Start playing a WAV and return immediately."""

    if _PLAYER is None:
        return
    try:
        subprocess.Popen(
            [*_PLAYER, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        log.debug("failed to launch audio player for %s", path, exc_info=True)
