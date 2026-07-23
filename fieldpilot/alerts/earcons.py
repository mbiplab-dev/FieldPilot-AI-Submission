"""Earcons: short, category-specific audio patterns.

Each hazard category gets a distinct tone signature (frequency + rhythm) so a worker learns to
recognize the hazard type pre-attentively, before the spoken explanation finishes. Tones are
synthesized with numpy and written as WAV files on first run — no audio assets to ship.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np

from fieldpilot.alerts.audio import play_wav_async
from fieldpilot.core.types import HazardType

_SAMPLE_RATE = 44100

# category -> list of (frequency_hz, duration_s) segments. Distinct pitch/rhythm per hazard type.
_SIGNATURES: dict[str, list[tuple[float, float]]] = {
    HazardType.FALL.value: [(880, 0.12), (0, 0.05), (880, 0.12), (0, 0.05), (1175, 0.20)],
    HazardType.PPE_MISSING.value: [(587, 0.15), (0, 0.06), (784, 0.15)],
    HazardType.UNNOTICED_HAZARD.value: [(660, 0.10), (0, 0.04), (660, 0.10), (0, 0.04), (660, 0.10)],
    HazardType.PROXIMITY.value: [(494, 0.18), (392, 0.18)],
    "default": [(700, 0.15), (0, 0.05), (700, 0.15)],
}


def _tone(freq: float, dur: float) -> np.ndarray:
    n = int(_SAMPLE_RATE * dur)
    if freq <= 0 or n == 0:
        return np.zeros(n, dtype=np.float32)
    t = np.linspace(0, dur, n, endpoint=False)
    wave_ = np.sin(2 * np.pi * freq * t)
    # short fade in/out to avoid clicks.
    fade = min(200, n // 4)
    if fade > 0:
        env = np.ones(n)
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        wave_ *= env
    return (0.6 * wave_).astype(np.float32)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    ints = np.clip(samples, -1.0, 1.0)
    ints = (ints * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(b"".join(struct.pack("<h", s) for s in ints))


class EarconBank:
    def __init__(self, cfg):
        self.dir = Path(cfg.get("alerts.earcons_dir", "fieldpilot/alerts/earcons"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self._paths: dict[str, Path] = {}
        self._ensure_files()

    def _ensure_files(self) -> None:
        for category, segments in _SIGNATURES.items():
            path = self.dir / f"{category}.wav"
            if not path.exists():
                samples = np.concatenate([_tone(f, d) for f, d in segments])
                _write_wav(path, samples)
            self._paths[category] = path

    def play(self, category: str) -> None:
        path = self._paths.get(category, self._paths["default"])
        play_wav_async(str(path))
