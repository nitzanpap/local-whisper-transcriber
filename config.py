"""Paths, constants, persisted settings, and the two pipeline exceptions."""

from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).parent
WEB_DIR = HERE / "web"
DATA_DIR = Path(os.environ.get("LWT_DATA_DIR", Path.home() / ".local-whisper-transcriber"))
WORK_DIR = DATA_DIR / "work"
HISTORY = DATA_DIR / "history.jsonl"
SETTINGS = DATA_DIR / "settings.json"

BINARIES = ("ffmpeg", "ffprobe", "whisper-cli")
DEFAULT_EXTRA = "--temperature 0 --entropy-thold 3.0 --max-context 64"

# What a watched folder will pick up. Anything ffmpeg reads works when chosen by
# hand; this narrower list is what we are willing to transcribe unattended.
MEDIA_EXTS = (".m4a", ".mp3", ".wav", ".mp4", ".mov", ".aac", ".flac", ".ogg", ".webm", ".mkv")

TRANSCRIPT_SUFFIX = "-transcript"


class Failed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Cancelled(Exception):
    pass


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(values: dict) -> dict:
    """Persist non-empty values, leaving anything not mentioned untouched."""
    merged = settings() | {k: v for k, v in values.items() if v not in (None, "")}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged
