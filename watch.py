"""Source folders: where new recordings turn up.

Nothing here runs on a timer. The app looks once when it opens, says what it
found, and waits to be told to go ahead — so transcription only ever starts
because somebody asked for it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import jobs
from config import (AUDIO_EXTS, DEFAULT_EXTRA, HISTORY, MEDIA_EXTS, TRANSCRIPT_SUFFIX,
                    VIDEO_EXTS, settings, source_folders)
from transcribe import duration_seconds

QUIET_SECONDS = 120          # a file still being written is left alone
MAX_PER_SWEEP = 25           # one scan must not queue a hundred jobs at once
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
            str(path), model, output_folder_for(path), f"{path.stem}{TRANSCRIPT_SUFFIX}",
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


def output_folder_for(source: Path) -> str:
    """Where a transcript goes: the configured folder, or beside the recording."""
    chosen = settings().get("output_folder", "").strip()
    if chosen:
        folder = Path(chosen).expanduser()
        if folder.is_dir():
            return str(folder)
    return str(source.parent)


def pending() -> dict:
    """What is sitting in the source folders waiting to be transcribed.

    Answered on demand — when the app opens, or when asked again — never on a
    timer, and never acted on without being told to.
    """
    names, skipped, folders = [], [], []
    for raw in source_folders():
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            skipped.append(f"{raw}: not a folder any more")
            continue
        found, why = candidates(folder)
        names += [p.name for p in found]
        skipped += why
        if found:
            folders.append(str(folder))
    return {"count": len(names), "names": names, "skipped": skipped, "folders": folders}


async def queue_pending() -> dict:
    """Queue everything the source folders are holding."""
    queued, names, skipped = 0, [], []
    for raw in source_folders():
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            continue
        result = await queue_folder(folder)
        queued += result["queued"]
        names += result.get("names", [])
        skipped += result.get("skipped", [])
    return {"queued": queued, "names": names, "skipped": skipped}
