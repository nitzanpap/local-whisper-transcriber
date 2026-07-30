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
AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
MEDIA_EXTS = AUDIO_EXTS + VIDEO_EXTS

TRANSCRIPT_SUFFIX = "-transcript"


def source_folders() -> list[str]:
    """Folders to look in for new recordings. `watch_folders` was the old name."""
    conf = settings()
    return conf.get("source_folders") or conf.get("watch_folders") or []


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
    """Persist the values given, leaving anything not mentioned untouched.

    An empty string is a real value here — it is how a field is cleared. Callers
    must send only the keys they mean to change (see the route's exclude_unset),
    or a partial save would wipe everything it left out.
    """
    merged = settings() | values
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged
