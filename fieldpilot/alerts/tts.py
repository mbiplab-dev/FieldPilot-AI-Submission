"""Text-to-speech with a pluggable provider and an offline fallback.

The PRD specified Faster-Whisper for TTS, which is wrong — Whisper is speech-*to-text*. This module
provides real TTS: a cloud provider (ElevenLabs or Google) when configured and reachable, always
backed by a local espeak-ng fallback so alerts still speak on a disconnected site. Cloud results are
cached to disk, so a phrase synthesized once will replay offline.

`speak()` is blocking and is expected to be called from a worker thread (see AlertDispatcher).
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import requests

from fieldpilot.alerts.audio import play_wav_async
from fieldpilot.logging_.logger import get_logger

log = get_logger("fieldpilot.tts")


class TTS:
    def __init__(self, cfg):
        t = cfg.section("alerts").get("tts", {})
        self.provider = str(t.get("provider", "local")).lower()
        self.voice = str(t.get("voice", "en+m3"))
        self.rate_wpm = int(t.get("rate_wpm", 175))
        self.timeout_s = float(t.get("timeout_s", 3.0))
        self.cache_dir = Path(t.get("cache_dir", "data/tts_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._has_espeak = shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None
        if self.provider == "local" and not self._has_espeak:
            log.warning("local TTS requested but espeak-ng not found; trying pyttsx3")

    # ---- public --------------------------------------------------------------------------------
    def speak(self, text: str) -> None:
        if self.provider == "elevenlabs":
            if self._cloud_play(text, self._synth_elevenlabs, "el"):
                return
        elif self.provider == "google":
            if self._cloud_play(text, self._synth_google, "goog"):
                return
        self._speak_local(text)

    # ---- cloud ---------------------------------------------------------------------------------
    def _cache_path(self, text: str, tag: str, ext: str) -> Path:
        digest = hashlib.sha1(f"{tag}:{self.voice}:{text}".encode()).hexdigest()[:16]
        return self.cache_dir / f"{tag}_{digest}.{ext}"

    def _cloud_play(self, text: str, synth, tag: str) -> bool:
        cached = self._cache_path(text, tag, "mp3")
        if cached.exists():
            play_wav_async(str(cached))
            return True
        try:
            audio = synth(text)
        except Exception as exc:  # noqa: BLE001 — any cloud failure falls back to local.
            log.warning("cloud TTS (%s) failed: %s — falling back to local", self.provider, exc)
            return False
        if not audio:
            return False
        cached.write_bytes(audio)
        play_wav_async(str(cached))
        return True

    def _synth_elevenlabs(self, text: str) -> bytes | None:
        key = os.environ.get("ELEVENLABS_API_KEY")
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
        if not key or not voice_id:
            raise RuntimeError("ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID not set")
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": key, "accept": "audio/mpeg", "content-type": "application/json"},
            json={"text": text, "model_id": "eleven_turbo_v2"},
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        return resp.content

    def _synth_google(self, text: str) -> bytes | None:
        key = os.environ.get("GOOGLE_TTS_API_KEY")
        if not key:
            raise RuntimeError("GOOGLE_TTS_API_KEY not set")
        resp = requests.post(
            f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}",
            json={
                "input": {"text": text},
                "voice": {"languageCode": "en-US"},
                "audioConfig": {"audioEncoding": "MP3"},
            },
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        content = resp.json().get("audioContent")
        return base64.b64decode(content) if content else None

    # ---- local fallback ------------------------------------------------------------------------
    def _speak_local(self, text: str) -> None:
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak:
            try:
                subprocess.run(
                    [espeak, "-v", self.voice, "-s", str(self.rate_wpm), text],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                return
            except (OSError, subprocess.SubprocessError):
                log.debug("espeak-ng invocation failed", exc_info=True)
        self._speak_pyttsx3(text)

    def _speak_pyttsx3(self, text: str) -> None:
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate_wpm)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception:  # noqa: BLE001
            log.warning("all TTS backends failed; alert spoken text: %s", text)
