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
from config import HISTORY, MEDIA_EXTS, TRANSCRIPT_SUFFIX, settings
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
    if len(found) > MAX_PER_SWEEP:
        skipped.append(f"{len(found) - MAX_PER_SWEEP} more left for the next sweep")
        found = found[:MAX_PER_SWEEP]
    return found, skipped


async def queue_folder(folder: Path) -> dict:
    """Queue everything in a folder that has no transcript yet."""
    conf = settings()
    model = conf.get("default_model_path", "")
    if not model or not Path(model).is_file():
        return {"queued": 0, "skipped": ["no default model set — choose one on the Transcribe view"]}
    found, skipped = candidates(folder)
    for path in found:
        job = jobs.make_job(
            str(path), model, str(path.parent), f"{path.stem}{TRANSCRIPT_SUFFIX}",
            language=conf.get("default_language", "he"),
            extra_args=conf.get("default_extra_args", ""),
            vad_model=conf.get("vad_model_path", ""),
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
