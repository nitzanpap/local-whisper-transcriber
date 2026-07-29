"""Watched folders: transcribe new recordings without being asked.

This is the only part of the app that starts work on its own, so the rules about
what it will *not* touch matter more than the scan itself.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import jobs
from config import (AUDIO_EXTS, DEFAULT_EXTRA, HISTORY, MEDIA_EXTS, TRANSCRIPT_SUFFIX,
                    VIDEO_EXTS, settings)
from transcribe import duration_seconds

SWEEP_SECONDS = 300          # how often watched folders are re-scanned
QUIET_SECONDS = 120          # a file still being written is left alone
MAX_PER_SWEEP = 25           # a first scan must not queue a hundred jobs silently
MAX_DEPTH = 4                # deep enough for Zoom's folder-per-meeting layout


def _already_transcribed(path: Path) -> bool:
    stem = path.stem
    return any((path.parent / f"{stem}{TRANSCRIPT_SUFFIX}.{ext}").exists() for ext in ("txt", "srt"))


def _known_sources() -> set[str]:
    """Every source we have ever run, so a deleted transcript is not redone forever."""
    known = set()
    try:
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                known.add(json.loads(line)["source"])
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    return known


def candidates(folder: Path, now: float | None = None) -> tuple[list[Path], list[str]]:
    """Media in `folder` worth transcribing, plus a note of what was skipped."""
    now = time.time() if now is None else now
    found, skipped = [], []
    known = _known_sources()
    queued = {j["source"] for j in jobs.QUEUE} | {(jobs.JOB or {}).get("source")}
    try:
        walk = sorted(p for p in folder.rglob("*") if p.suffix.lower() in MEDIA_EXTS)
    except OSError:
        return [], [f"{folder} could not be read"]
    for path in walk:
        if len(path.relative_to(folder).parts) > MAX_DEPTH:
            continue
        if TRANSCRIPT_SUFFIX in path.stem or not path.is_file():
            continue
        if str(path) in queued:
            continue
        if _already_transcribed(path):
            continue
        if str(path) in known:
            skipped.append(f"{path.name}: already transcribed once")
            continue
        try:
            if now - path.stat().st_mtime < QUIET_SECONDS:
                skipped.append(f"{path.name}: still being written")
                continue
        except OSError:
            continue
        found.append(path)
    found, dropped = prefer_audio(found)
    skipped += dropped
    if len(found) > MAX_PER_SWEEP:
        skipped.append(f"{len(found) - MAX_PER_SWEEP} more left for the next sweep")
        found = found[:MAX_PER_SWEEP]
    return found, skipped


def prefer_audio(found: list[Path]) -> tuple[list[Path], list[str]]:
    """Zoom writes audio and video of the same meeting side by side.

    Transcribing both is the same words twice at twice the cost, and the audio
    file is the better input, so a video is skipped when its folder also holds
    audio. A folder with only video is still transcribed.
    """
    has_audio: dict[Path, bool] = {}
    keep, dropped = [], []
    for path in found:
        if path.suffix.lower() not in VIDEO_EXTS:
            keep.append(path)
            continue
        # Ask the folder, not the candidate list: the audio file is usually absent
        # from the list precisely because it was already transcribed.
        if path.parent not in has_audio:
            try:
                has_audio[path.parent] = any(s.suffix.lower() in AUDIO_EXTS
                                             for s in path.parent.iterdir() if s.is_file())
            except OSError:
                has_audio[path.parent] = False
        if has_audio[path.parent]:
            dropped.append(f"{path.name}: audio of the same recording is in this folder")
        else:
            keep.append(path)
    return keep, dropped


async def queue_folder(folder: Path, dry_run: bool = False) -> dict:
    """Queue everything in a folder that has no transcript yet.

    dry_run answers "what would this pick up?" without starting anything, which
    is the only safe way to point a watcher at a folder you cannot see into.
    """
    conf = settings()
    model = conf.get("default_model_path", "")
    if not model or not Path(model).is_file():
        return {"queued": 0, "skipped": ["no default model set — choose one on the Transcribe view"]}
    found, skipped = candidates(folder)
    if dry_run:
        return {"queued": 0, "would_queue": len(found), "skipped": skipped,
                "names": [p.name for p in found]}
    for path in found:
        job = jobs.make_job(
            str(path), model, str(path.parent), f"{path.stem}{TRANSCRIPT_SUFFIX}",
            language=conf.get("default_language", "he"),
            # Fall back to the same defaults the form uses; an unattended job must
            # not quietly run with different settings from a hand-started one.
            extra_args=conf.get("default_extra_args") or DEFAULT_EXTRA,
            vad_model=conf.get("vad_model_path", ""),
            vocabulary=conf.get("vocabulary", ""),
            duration=await duration_seconds(path),
        )
        jobs.enqueue(job)
    return {"queued": len(found), "skipped": skipped,
            "names": [p.name for p in found]}


async def watcher() -> None:
    """Re-scan the watched folders forever, one sweep at a time."""
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        for raw in settings().get("watch_folders", []):
            folder = Path(raw).expanduser()
            if not folder.is_dir():
                continue
            try:
                await queue_folder(folder)
            except Exception:  # noqa: BLE001 - a bad folder must not kill the watcher
                continue
