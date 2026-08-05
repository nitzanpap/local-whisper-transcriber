"""What a recording knew about itself, written down before it is forgotten.

Every fault in this project so far was diagnosed by hand, afterwards, from a file
and a person's memory — and each time the thing that would have answered it in a
second had existed and been thrown away. The recording's own log, holding the
helper's every word and the exact ffmpeg commands, lives in memory and dies with
the scratch directory. Which device was actually opened is never written anywhere:
the Teams-loopback fault cost hours because nobody could see that the computer's
side had been pointed at a driver carrying nothing. The measured levels are taken
at save time and used for one warning and then dropped. Whether a channel has
holes in it was never asked at all, and that is the fault that has now been found
twice by ear.

So this takes one pass over the finished recording, adds what only the recorder
knew, and appends a line to `recordings.jsonl` beside the job history. It is
written after the .m4a is safely in place and never allowed to fail the recording:
a diagnostic that can lose somebody's meeting is worse than no diagnostic.

    python3 tools/diagnose.py            what the last recording knows
    python3 tools/diagnose.py --all      every one still on file
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from pathlib import Path

from config import DATA_DIR
from tools import binary

RECORDINGS = DATA_DIR / "recordings.jsonl"

# 100 ms of a 48 kHz stream, matching the window `levels` uses, so a number here
# means the same as a number there.
WINDOW = 4800
RMS_LEVEL = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?\d+(?:\.\d+)?|-?inf)")

# A window at or below this held nothing at all. The tap's floor is digital zero
# between sounds, so this is not "quiet", it is "the helper wrote nothing here".
NOTHING = -100.0

# A hole shorter than this is the ordinary edge between a sound and a silence.
SHORTEST_HOLE = 0.3


def _windows(path: Path, channel: int) -> list[float]:
    """Loudness per 100 ms, in time order — the order is the whole point here."""
    try:
        done = subprocess.run(
            [binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
             "-af", f"pan=mono|c0=c{channel},aresample=48000,asetnsamples=n={WINDOW}:p=0,"
                    "astats=metadata=1:reset=1,"
                    "ametadata=print:key=lavfi.astats.Overall.RMS_level",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError):
        return []
    return [-120.0 if "inf" in raw else float(raw) for raw in RMS_LEVEL.findall(done.stderr)]


def holes(values: list[float]) -> list[dict]:
    """Stretches where nothing arrived, with sound on both sides of them.

    Bounded by real audio on purpose. A recording that starts before anybody
    speaks and ends after they stop is not broken, and counting those would bury
    the thing worth seeing: a capture that stopped delivering *while it was
    delivering*, which is what a device changing underneath a tap looks like.
    """
    live = [n for n, value in enumerate(values) if value > NOTHING]
    if len(live) < 2:
        return []
    found, run = [], 0
    for n in range(live[0], live[-1] + 1):
        if values[n] <= NOTHING:
            run += 1
            continue
        if run * 0.1 >= SHORTEST_HOLE:
            found.append({"at": round((n - run) * 0.1, 1), "seconds": round(run * 0.1, 1)})
        run = 0
    return found


def channel_report(path: Path, sources: list[str]) -> dict:
    """One entry per side: how loud, how far above its own floor, and where it broke."""
    out = {}
    for index, side in enumerate(sources):
        values = _windows(path, index if len(sources) > 1 else 0)
        if not values:
            out[side] = {"measured": False}
            continue
        ordered = sorted(values)
        floor = ordered[int(len(ordered) * 0.10)]
        speech = ordered[int(len(ordered) * 0.90)]
        broken = holes(values)
        out[side] = {
            "measured": True,
            "floor_db": round(floor, 1),
            "speech_db": round(speech, 1),
            "snr_db": round(speech - floor, 1),
            "silent_seconds": round(sum(1 for v in values if v <= NOTHING) * 0.1, 1),
            "seconds": round(len(values) * 0.1, 1),
            # The measurement that has now caught the same class of fault twice.
            "holes": broken,
            "hole_seconds": round(sum(h["seconds"] for h in broken), 1),
        }
    return out


def _versions() -> dict:
    out = {"platform": platform.platform()}
    for name in ("ffmpeg", "whisper-cli"):
        try:
            found = binary(name)
        except Exception:                                   # noqa: BLE001 — never fatal
            out[name] = "not found"
            continue
        try:
            said = subprocess.run([found, "-version" if name == "ffmpeg" else "--help"],
                                  capture_output=True, text=True, timeout=20).stdout
            out[name] = said.splitlines()[0][:120] if said else found
        except (OSError, subprocess.SubprocessError):
            out[name] = found
    return out


def about(rec: dict) -> dict:
    """Everything worth keeping about one finished recording.

    The devices are written by both id and name. An id alone is what the
    Teams-loopback fault looked like from the outside — a stored string nobody
    could read as "this is not the computer's audio at all".
    """
    path = Path(rec["path"]) if rec.get("path") else None
    sources = rec.get("sources") or []
    return {
        "id": rec.get("id"),
        "path": str(path) if path else None,
        "started_at": rec.get("started_at"),
        "ended_at": rec.get("ended_at"),
        "status": rec.get("status"),
        "job_id": rec.get("job_id"),
        "asked_for": rec.get("devices") or [],
        "devices": {
            "voice": {"id": rec.get("voice"), "name": (rec.get("device_names") or {}).get("voice")},
            "computer": {"id": rec.get("computer"),
                         "name": (rec.get("device_names") or {}).get("computer")},
        },
        # Which output the tap was built on. It is fixed for the life of the
        # recording, and a recording where this stops matching the machine's
        # default output is a recording whose computer side has gone deaf.
        "output_device": rec.get("output_device"),
        "captured": sources,
        "labels": list(rec.get("labels") or []),
        "helper_exit": rec.get("helper_code"),
        "helper_used": bool(rec.get("helper")),
        # Stalls the app noticed live, against holes measured in the file
        # afterwards. They should agree; when they do not, one of the two is the
        # bug and knowing which is half the work.
        "stalls_seen": {side: list(map(list, runs))
                        for side, runs in (rec.get("stalls") or {}).items()},
        "padded": {side: list(map(list, runs))
                   for side, runs in (rec.get("gaps") or {}).items()},
        # What the helper itself said it was writing in place of audio. Ground
        # truth: nothing that reads the finished file can recover this.
        "padding": rec.get("padding") or {},
        "levels": rec.get("levels") or {},
        "snr": rec.get("snr") or {},
        "quiet": rec.get("quiet") or [],
        "noisy": rec.get("noisy") or [],
        "channels": channel_report(path, sources) if path and path.is_file() and sources else {},
        "versions": _versions(),
        "log": list(rec.get("log") or []),
    }


def remember(rec: dict) -> None:
    """Append one line. Never raises — a diagnostic must not lose a meeting."""
    try:
        line = json.dumps(about(rec), ensure_ascii=False, default=str)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with RECORDINGS.open("a", encoding="utf-8") as sink:
            sink.write(line + "\n")
    except Exception:                                       # noqa: BLE001 — on purpose
        pass


def from_file(path: Path) -> dict:
    """As much as can still be told from a recording made before any of this existed.

    The log and the devices are gone — they only ever lived in memory — but the
    channels are still in the file, and so is every hole in them. Enough to answer
    the question that has now cost two evenings: did a capture stop while it was
    still delivering.
    """
    channels = 2
    try:
        done = subprocess.run(
            [binary("ffprobe"), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels", "-of",
             "default=nokey=1:noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=60)
        channels = int(done.stdout.strip() or 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        channels = 1
    sources = ["voice", "computer"][:channels]
    try:
        started = path.stat().st_mtime
    except OSError:
        started = None
    return {
        "id": None, "path": str(path), "started_at": started, "ended_at": started,
        "status": "saved", "job_id": None,
        "asked_for": sources, "captured": sources,
        # Not recorded at the time. Said as unknown rather than left out, because a
        # blank that looks like an answer is worse than one that says it is blank.
        "devices": {"voice": {"id": None, "name": "not recorded at the time"},
                    "computer": {"id": None, "name": "not recorded at the time"}},
        "output_device": None, "helper_exit": None, "helper_used": None,
        "stalls_seen": {}, "padded": {}, "levels": {}, "snr": {},
        "quiet": [], "noisy": [],
        "channels": channel_report(path, sources),
        "versions": _versions(),
        "log": ["# reconstructed from the file; the original log was not kept"],
    }


def backfill(folder: Path) -> int:
    """Write a record for every recording in `folder` that has none."""
    known = {row.get("path") for row in recent(500)}
    written = 0
    for path in sorted(folder.glob("*.m4a")):
        if str(path) in known:
            continue
        try:
            line = json.dumps(from_file(path), ensure_ascii=False, default=str)
        except Exception:                                   # noqa: BLE001
            continue
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with RECORDINGS.open("a", encoding="utf-8") as sink:
            sink.write(line + "\n")
        written += 1
    return written


def recent(limit: int = 20) -> list[dict]:
    """What was written down, newest first."""
    try:
        lines = RECORDINGS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def trim(keep: int = 200) -> None:
    """Keep the file from growing without limit. Called after each append."""
    try:
        lines = RECORDINGS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= keep:
        return
    spare = RECORDINGS.with_suffix(".jsonl.new")
    spare.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    shutil.move(str(spare), str(RECORDINGS))
