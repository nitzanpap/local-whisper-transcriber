"""Turning media into segments, and segments into transcripts.

whisper-cli prints one line per finished segment with absolute timestamps, even
when started at an offset. That single fact is why resume needs no chunking: a
resumed run simply appends to the same segment stream.
"""

from __future__ import annotations

import re
import shutil
from difflib import SequenceMatcher
from pathlib import Path

from config import Failed
from tools import REGION_LINE, binary, capture, stream

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


async def to_wav(job: dict, source: str, wav: Path, channel: int | None = None) -> None:
    """16 kHz mono, which is what whisper wants — optionally from one channel only.

    A recording made here puts the microphone in the left channel and the
    computer's own audio in the right, so pulling one channel out is pulling one
    speaker out. Without a channel this is the plain downmix it always was.
    """
    cmd = [binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-vn"]
    if channel is not None:
        cmd += ["-filter_complex", f"[0:a]pan=mono|c0=c{channel}[a]", "-map", "[a]"]
    await stream(
        cmd + ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
        job, "ffmpeg_failed",
    )


# How alike two lines have to be before one is taken for an echo of the other.
# Generous, because the microphone's copy has been through a speaker and a room and
# comes back with words softened or dropped.
ECHO_SIMILARITY = 0.75


def _bare(text: str) -> str:
    """Words only, lowercased, for comparing what was said rather than how."""
    return " ".join(re.findall(r"\w+", text.lower()))


def drop_echo(tracks: list[tuple[str, list[tuple[int, int, str]]]]
              ) -> list[tuple[str, list[tuple[int, int, str]]]]:
    """Remove the microphone's copy of whatever the speakers were playing.

    Recording without headphones means the microphone hears the computer, so the
    same sentence arrives on both channels and is transcribed twice — once as the
    person and once as the machine. It is not a fault in the capture and cannot be
    fixed there: a microphone in a room with a speaker hears the speaker.

    The computer's channel is kept because it is the better copy. It was taken
    digitally, before any of it reached the air; the microphone's version has been
    through a speaker, a room and back, and is the one that comes out garbled. Only
    the microphone's side is thinned, and only where the machine was saying the same
    thing at the same time, so anyone speaking over the audio keeps their line.
    """
    if len(tracks) < 2:
        return tracks
    (voice_label, voice_segments), *rest = tracks
    others = [seg for _, segments in rest for seg in segments]
    kept = []
    for start, end, text in voice_segments:
        spoken = _bare(text)
        echo = any(
            other_start < end and start < other_end
            and SequenceMatcher(None, spoken, _bare(other_text)).ratio() >= ECHO_SIMILARITY
            for other_start, other_end, other_text in others)
        if not echo:
            kept.append((start, end, text))
    return [(voice_label, kept), *rest]


def merge_tracks(tracks: list[tuple[str, list[tuple[int, int, str]]]]) -> list[tuple[int, int, str]]:
    """Several tracks' segments as one stream, each line owned by whoever said it.

    Both tracks carry absolute timestamps from the same recording, so ordering by
    start time interleaves them the way the conversation actually went. A track
    with no label contributes its text unchanged, which is the single-track case.
    """
    merged = []
    for label, segments in drop_echo(tracks):
        for start, end, text in segments:
            merged.append((start, end, f"{label}: {text}" if label else text))
    merged.sort(key=lambda seg: (seg[0], seg[1]))
    return merged


def whisper_command(job: dict, wav: Path, resume_ms: int = 0) -> list[str]:
    cmd = [binary("whisper-cli"), "-m", job["model"], "-f", str(wav),
           "-l", job["language"], "--print-progress"]
    if resume_ms:
        # Timestamps stay absolute across an offset, so resumed output needs no
        # stitching — it simply continues the same segment file.
        cmd += ["--offset-t", str(resume_ms)]
    # Voice activity detection, on for every job that has a model — and the reason
    # it was once kept off multi-track jobs was a misreading.
    #
    # VAD's own placement is right. Measured on a file built to prove it: speech at
    # 1.006 s and 13.061 s came back as regions at 0.96 and 13.09, inside 100 ms.
    # What went wrong is that VAD removes the silence between them, so the two
    # utterances become adjacent and whisper emits them as ONE segment holding both
    # — 00:00.960 to 00:13.370, "1, 5." — and a segment spanning the recording sorts
    # ahead of everything on the other channel.
    #
    # -ml 1 -sow is the cure: one segment per word, each carrying its own time. They
    # are grouped back into sentences by `regroup`, against the regions whisper
    # reported on its way past. Measured cost of the split: 0.05 s on a 300 s file,
    # and inside the noise on a dense one.
    #
    # Turning VAD off is not a lesser version of this, it is no version of it:
    # measured, -ml 1 -sow without --vad reproduces the same 11-second spans. See
    # regroup, and TIMING.md.
    if job.get("vad_model"):
        cmd += ["--vad", "--vad-model", job["vad_model"], "--max-len", "1", "--split-on-word"]
    if job.get("vocabulary", "").strip():
        # --prompt alone primes only the first window, which on a 40-minute meeting
        # is the first half minute. Carrying it applies the vocabulary throughout.
        cmd += ["--prompt", job["vocabulary"].strip(), "--carry-initial-prompt"]
    return cmd + job["extra_args"]


async def transcribe(job: dict, wav: Path, segments_file: Path, resume_ms: int = 0,
                     regions_file: Path | None = None) -> None:
    await stream(whisper_command(job, wav, resume_ms), job, "whisper_failed",
                 capture_to=segments_file, regions_to=regions_file)


# How the words come back together into sentences.
GAP_MS = 700          # a silence inside one region long enough to end a sentence
MAX_SPAN_MS = 12_000  # nothing may run longer than this; a region can be 23 s of talk
SLACK_MS = 400        # how far outside a region a word may start and still belong to it


def parse_regions(path: Path) -> list[tuple[int, int]]:
    """Where whisper's VAD found speech, in the original file's time, as ms pairs."""
    out: list[tuple[int, int]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        found = REGION_LINE.match(line.strip())
        if found:
            out.append((round(float(found.group(1)) * 1000),
                        round(float(found.group(2)) * 1000)))
    return sorted(out)


def _coalesce(regions: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Join regions with no real silence between them.

    A region boundary is only worth splitting a sentence at when somebody actually
    stopped talking. VAD reports one per breath, so "Hello there" comes back as two
    regions a few tens of milliseconds apart — and splitting there put "Hello" and
    "there." on separate lines with separate timestamps. The boundaries that matter
    are the ones with a pause in them, which is the same threshold a pause inside a
    region has to clear.
    """
    out: list[tuple[int, int]] = []
    for start, end in sorted(regions):
        if out and start - out[-1][1] < GAP_MS:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _region_for(regions: list[tuple[int, int]], at: int) -> int | None:
    """Which region a word starting at `at` belongs to."""
    best, best_gap = None, None
    for index, (start, end) in enumerate(regions):
        if start - SLACK_MS <= at <= end + SLACK_MS:
            return index
        gap = start - at if at < start else at - end
        if best_gap is None or gap < best_gap:
            best, best_gap = index, gap
    return best


def regroup(words: list[tuple[int, int, str]],
            regions: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """One segment per word, back into one segment per thing somebody said.

    Both halves matter. The words carry whisper's own timing, which is good inside a
    stretch of speech and meaningless across a silence it never saw. The regions are
    measured, and they are where a sentence must not be allowed to run past.

    Without this, an eighteen-second recording came back as `00:00 --> 00:12` for a
    word said at 1.0 s and `00:12 --> 00:21` for one said at 13.1 s — past the end
    of the file. With it: 1.010 and 13.120, against a truth of 1.006 and 13.061.
    """
    if not regions:
        return words
    regions = _coalesce(regions)
    out: list[tuple[int, int, str]] = []
    bucket: list[tuple[int, int, str]] = []
    where: int | None = None

    def flush() -> None:
        if not bucket or where is None:
            return
        region_start, region_end = regions[where]
        start = min(word[0] for word in bucket)
        end = max(word[1] for word in bucket)
        # The region is measured; the word times are whisper's estimate. Trust the
        # region for the outer bounds and the words for everything inside — this is
        # what stops a segment ending after the recording does.
        start = max(min(start, region_end), region_start)
        end = min(max(end, start), region_end)
        text = re.sub(r"\s+([,.!?;:])", r"\1", " ".join(w[2] for w in bucket)).strip()
        if text:
            out.append((start, end, text))
        bucket.clear()

    for start, end, text in words:
        index = _region_for(regions, start)
        if index is not None:
            # Clamp to the region before deciding anything, because a word's end is
            # not to be believed: whisper runs the last word of a sentence all the
            # way to the first word of the next one, across the silence between. Left
            # unclamped that made a two-word phrase look twelve seconds long, and the
            # rule meant to break up a monologue split "Hello" from "there."
            region_start, region_end = regions[index]
            start = max(min(start, region_end), region_start)
            end = min(max(end, start), region_end)
        if where is not None and (index != where
                                  or start - bucket[-1][1] >= GAP_MS
                                  or end - bucket[0][0] >= MAX_SPAN_MS):
            flush()
        where = index
        bucket.append((start, end, text))
    flush()
    out.sort(key=lambda seg: (seg[0], seg[1]))
    return out


def write_outputs(job: dict, work: Path, segments: list[tuple[int, int, str]]) -> None:
    """Write the requested formats aside, then move them into place."""
    if not segments:
        # Not malformed output, which is what this used to say and what sent one
        # investigation looking for a broken pipeline. whisper recognised nothing,
        # and by far the likeliest reason is that there was nothing to recognise:
        # a source too quiet to be speech, or the wrong input device chosen.
        raise Failed("no_speech_found",
                     "No speech was recognised in this recording. If it was recorded here, "
                     "check that the right microphone is selected — an input that is silent or "
                     "very quiet gets this far and then transcribes to nothing.")
    out_dir = Path(job["out_dir"])
    for ext in ("txt", "srt"):
        if not job[f"want_{ext}"]:
            continue
        staged = work / f"final.{ext}"
        WRITERS[ext](segments, staged)
        final = out_dir / f"{job['basename']}.{ext}"
        shutil.move(str(staged), str(final))  # handles a different filesystem
        job["outputs"][ext] = str(final)
