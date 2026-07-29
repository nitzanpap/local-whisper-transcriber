"""Turning media into segments, and segments into transcripts.

whisper-cli prints one line per finished segment with absolute timestamps, even
when started at an offset. That single fact is why resume needs no chunking: a
resumed run simply appends to the same segment stream.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from config import Failed
from tools import binary, capture, stream

# whisper-cli prints "[00:00:20.000 --> 00:00:29.980]   text" per finished segment.
SEGMENT_RE = re.compile(
    r"^\[(\d+):(\d\d):(\d\d)\.(\d{3}) --> (\d+):(\d\d):(\d\d)\.(\d{3})\]\s?(.*)$"
)


def parse_segments(path: Path) -> list[tuple[int, int, str]]:
    """Absolute (start_ms, end_ms, text) triples. Malformed lines are dropped.

    A line half-written when the process died fails the pattern, so a truncated
    file simply yields one segment fewer.
    """
    out: list[tuple[int, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = SEGMENT_RE.match(line.strip())
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2, body = m.groups()
        start = ((int(h1) * 60 + int(m1)) * 60 + int(s1)) * 1000 + int(ms1)
        end = ((int(h2) * 60 + int(m2)) * 60 + int(s2)) * 1000 + int(ms2)
        if end >= start and body.strip():
            out.append((start, end, body.strip()))
    return out


def stamp(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def write_txt(segments: list[tuple[int, int, str]], path: Path) -> None:
    path.write_text("\n".join(t for _, _, t in segments) + "\n", encoding="utf-8")


def write_srt(segments: list[tuple[int, int, str]], path: Path) -> None:
    blocks = [
        f"{i}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(segments, 1)
    ]
    # Trailing blank line terminates the last block, the way whisper's own writer
    # does; strict SRT parsers expect it. Text is stripped of whisper's leading
    # space, which is the one deliberate difference from its output.
    path.write_text("\n".join(blocks) + "\n", encoding="utf-8")


WRITERS = {"txt": write_txt, "srt": write_srt}

SRT_TIME_RE = re.compile(r"^(\d+):(\d\d):(\d\d),(\d{3}) --> (\d+):(\d\d):(\d\d),(\d{3})")


def parse_srt(path: Path) -> list[tuple[int, int, str]]:
    """Read an .srt back into the same triples write_srt produced."""
    out: list[tuple[int, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        m = SRT_TIME_RE.match(lines[1])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        start = ((int(h1) * 60 + int(m1)) * 60 + int(s1)) * 1000 + int(ms1)
        end = ((int(h2) * 60 + int(m2)) * 60 + int(s2)) * 1000 + int(ms2)
        out.append((start, end, " ".join(lines[2:]).strip()))
    return out


async def duration_seconds(path: Path) -> float | None:
    code, out = await capture(
        [binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)]
    )
    try:
        return round(float(out.splitlines()[0]), 2) if code == 0 else None
    except (ValueError, IndexError):
        return None


async def to_wav(job: dict, source: str, wav: Path) -> None:
    await stream(
        [binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", source,
         "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
        job, "ffmpeg_failed",
    )


def whisper_command(job: dict, wav: Path, resume_ms: int = 0) -> list[str]:
    cmd = [binary("whisper-cli"), "-m", job["model"], "-f", str(wav),
           "-l", job["language"], "--print-progress"]
    if resume_ms:
        # Timestamps stay absolute across an offset, so resumed output needs no
        # stitching — it simply continues the same segment file.
        cmd += ["--offset-t", str(resume_ms)]
    if job.get("vad_model"):
        # Voice activity detection: skips silence, which is where whisper likes to
        # invent text. Off unless a VAD model is configured.
        cmd += ["--vad", "--vad-model", job["vad_model"]]
    return cmd + job["extra_args"]


async def transcribe(job: dict, wav: Path, segments_file: Path, resume_ms: int = 0) -> None:
    await stream(whisper_command(job, wav, resume_ms), job, "whisper_failed", capture_to=segments_file)


def write_outputs(job: dict, work: Path, segments: list[tuple[int, int, str]]) -> None:
    """Write the requested formats aside, then move them into place."""
    if not segments:
        raise Failed("malformed_chunk_output", "whisper-cli produced no transcript segments")
    out_dir = Path(job["out_dir"])
    for ext in ("txt", "srt"):
        if not job[f"want_{ext}"]:
            continue
        staged = work / f"final.{ext}"
        WRITERS[ext](segments, staged)
        final = out_dir / f"{job['basename']}.{ext}"
        shutil.move(str(staged), str(final))  # handles a different filesystem
        job["outputs"][ext] = str(final)
