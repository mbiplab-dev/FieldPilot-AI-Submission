#!/usr/bin/env python3
"""Fast, read-only preflight for a predictable FieldPilot hackathon demonstration."""

from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(label: str, ok: bool, detail: str) -> bool:
    mark = "OK" if ok else "MISSING"
    print(f"{mark:<7} {label:<20} {detail}")
    return ok


def check_optional(label: str, ok: bool, detail: str) -> None:
    mark = "OK" if ok else "OPTIONAL"
    print(f"{mark:<7} {label:<20} {detail}")


def ollama_models() -> set[str]:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as response:
            payload = json.load(response)
        return {str(model.get("name")) for model in payload.get("models", [])}
    except Exception:
        return set()


def main() -> int:
    print("FieldPilot hackathon preflight\n")
    required = [
        check("Python environment", shutil.which("uv") is not None, "uv available"),
        check("Pose detector", (ROOT / "models/yolo11m-pose.pt").is_file(),
              "models/yolo11m-pose.pt"),
        check("PPE detector", (ROOT / "models/ppe_css.pt").is_file(), "models/ppe_css.pt"),
    ]
    models = ollama_models()
    required.append(check("Gemma assistant", "gemma4:e4b-it-qat" in models,
                          "gemma4:e4b-it-qat in Ollama"))
    check("Frontend", (ROOT / "frontend/node_modules/.bin/next").is_file(),
          "npm ci" if not (ROOT / "frontend/node_modules/.bin/next").is_file() else "installed")
    apk = ROOT / "worker_app/build/app/outputs/flutter-apk/app-debug.apk"
    required.append(check("Worker APK", apk.is_file(),
                          str(apk.relative_to(ROOT)) if apk.is_file()
                          else "build the Android debug APK before the demo"))
    check_optional("Flutter SDK", shutil.which("flutter") is not None,
                   "only required to rebuild the prepared APK")
    check_optional("Docker", shutil.which("docker") is not None,
                   "SQLite fallback is supported")

    if all(required):
        print("\nCore demo assets are ready. Run: make run-all")
        return 0
    print("\nCore assets are incomplete. Run: make setup && make fetch-models && make llm-pull")
    return 1


if __name__ == "__main__":
    sys.exit(main())
