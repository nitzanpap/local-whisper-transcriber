"""Paths, constants, persisted settings, and the two pipeline exceptions."""

from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).parent
WEB_DIR = HERE / "web"

# Shipped, not fetched. Without a VAD model every transcript comes back with
# invented timestamps — measured, the same eleven-second spans that started all
# this — and nothing about the result says so. It is 864 KB beside a model the
# user already downloads in gigabytes, and settings used to hand them a curl
# command to run by hand for it. See docs/PIPELINE.md.
BUNDLED_VAD = HERE / "mac" / "models" / "ggml-silero-v5.1.2.bin"


def vad_model() -> str:
    """The VAD model to use: whatever was chosen, else the one we ship."""
    chosen = settings().get("vad_model_path", "")
    if chosen and Path(chosen).expanduser().is_file():
        return str(Path(chosen).expanduser())
    return str(BUNDLED_VAD) if BUNDLED_VAD.is_file() else ""
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

# Scratch directories for recordings are named so they can be told apart from
# job directories, which the sweep treats far less gently. Lives here because
# both the recorder and the sweep need it and neither may import the other.
RECORDING_PREFIX = "rec-"

# Recording defaults. Every one of them is a Settings field.
RECORD_FOLDER = Path.home() / "Recordings"
RECORD_MAX_MINUTES = 180
RECORD_LABELS = ("Me", "Them")
RECORD_MAX_MINUTES_CEILING = 12 * 60


def recording_config() -> dict:
    """The recording settings, resolved, with every value usable as it stands."""
    conf = settings()
    folder = (conf.get("recording_folder") or "").strip()
    try:
        minutes = max(1, min(int(conf.get("record_max_minutes") or RECORD_MAX_MINUTES),
                             RECORD_MAX_MINUTES_CEILING))
    except (TypeError, ValueError):
        minutes = RECORD_MAX_MINUTES
    labels = ((conf.get("record_label_voice") or "").strip() or RECORD_LABELS[0],
              (conf.get("record_label_computer") or "").strip() or RECORD_LABELS[1])
    return {
        "folder": str(Path(folder).expanduser()) if folder else str(RECORD_FOLDER),
        "voice": conf.get("record_voice_device") or "",
        "computer": conf.get("record_computer_device") or "",
        "labels": labels,
        # Off unless explicitly turned on. Transcribing is the expensive half and
        # the moment a meeting ends is often the worst time to spend a machine on
        # it; the recording is safe on disk either way and the Library will
        # transcribe it whenever asked. This was on by default once, which meant a
        # long recording seized the machine the instant it was saved.
        "transcribe": conf.get("record_auto_transcribe") is True,
        "max_seconds": minutes * 60,
        "max_minutes": minutes,
    }


def source_folders() -> list[str]:
    """Folders to look in for new recordings. `watch_folders` was the old name."""
    conf = settings()
    return conf.get("source_folders") or conf.get("watch_folders") or []


class Failed(Exception):
    def __init__(self, code: str, message: str, pane: str = ""):
        super().__init__(message)
        # `pane` names a macOS privacy pane to offer to open. Naming the pane in a
        # sentence is not the same as getting somebody to it, and the difference is
        # a person hunting through System Settings while a meeting starts.
        self.code, self.message, self.pane = code, message, pane


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
