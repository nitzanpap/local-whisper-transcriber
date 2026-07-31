"""Recording: your voice and your computer's audio, into one file.

macOS hands an app the microphone or nothing — there is no system-audio input
device until a loopback driver (BlackHole and friends) provides one. And an
Aggregate Device does not help by itself: it *concatenates* channels rather than
mixing them, which is why recorders fed one come back with the mic alone.

So this opens two devices at once and does the mixing here, keeping the two
apart: your voice in the left channel, everything else in the right. That is
what makes a labelled transcript possible afterwards — each channel is
transcribed on its own, so who said a line is known rather than guessed.

The master is a WAV in scratch, not the .m4a that gets kept. A WAV's header
comes first, so a recording cut short by a crash is still playable; an .m4a
without its trailing index is nothing at all. Stopping transcodes it into place.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
import uuid
from collections import deque
from pathlib import Path

import jobs
import watch
from config import (DATA_DIR, DEFAULT_EXTRA, Failed, RECORDING_PREFIX, TRANSCRIPT_SUFFIX,
                    WORK_DIR, recording_config, save_settings, settings)
from tools import binary, capture
from transcribe import duration_seconds

# The one being made, if any. Its whole life happens in TASK.
RECORDING: dict | None = None
TASK: asyncio.Task | None = None
# Our own child. Deliberately not tools.PROC: cancelling a transcription must
# not stop a recording, and stopping a recording must not stop a transcription.
PROC: asyncio.subprocess.Process | None = None
# Every capture running now: one per real device. PROC is the first of them, kept
# because _insist and the tests ask whether "the" child is still alive.
PROCS: list[asyncio.subprocess.Process] = []
# The system-audio helper, when one is feeding ffmpeg. Stopped with ffmpeg, and
# separately, because it is a sibling of the recording rather than a child of it.
HELPER: asyncio.subprocess.Process | None = None

LIVE = ("recording", "paused", "stopping", "saving")
MAX_LOG = 120

# A WAV shorter than this is a header and nothing else.
EMPTY_WAV = 2048

# A recording is stopped once the disk has less than this left, which is roughly
# ten minutes of two sources. Stopping early keeps a meeting that is mostly there;
# filling the disk loses the end of it and can take the machine down with it.
LOW_DISK = 400_000_000


def disk_is_low(free_bytes: int) -> bool:
    return free_bytes < LOW_DISK


# The helper's own exit code for a permission macOS would not give. Kept in step
# with DENIED in mac/syscapture.swift.
HELPER_DENIED = 3

# ebur128's momentary loudness. The filter computes it but prints nothing on its
# own; ametadata is what puts it on stderr, a line at a time, where the log is
# already being read. Measured on real speech at about -25 LUFS, and -120 for
# digital silence.
EBUR128_M = re.compile(r"lavfi\.r128\.M=(-?\d+(?:\.\d+)?)")

# What the needle moves on. `-inf` is what astats calls a window of digital zero,
# and it is read as the same -120 the helper has always used for that, so both
# sides of a recording say "nothing at all" in the same words.
PEAK = re.compile(r"lavfi\.astats\.Overall\.Peak_level=(-?\d+(?:\.\d+)?|-?inf)")
DIGITAL_SILENCE = -120.0
SILENT_LUFS = -70.0

# The helper's own meter, once a second, so the computer's side has one too.
# The helper captures both sides now, so it says which one it is speaking for.
HELPER_LEVEL = re.compile(r"syscapture: (\w+) level (-?\d+(?:\.\d+)?) frames (\d+)")
# Digital zero, which the helper reports as -120 and no real signal ever reaches.
# Frames arriving with nothing in them at all is what a refused tap looks like;
# frames not arriving is only a quiet machine.
DEAD_TAP = -100.0

# What a device is called when it exists to carry audio back into the machine.
# Used to preselect the right one and to notice when there is none.
LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "vb-cable", "vb cable",
                  "aggregate", "multi-output", "monitor of", ".monitor")


# --- the computer's own audio, without a driver -------------------------------


# Offered in place of a loopback device, and not an avfoundation index: nothing
# opens it, because capture_command turns it into a pipe from the helper instead.
# It is presented as a loopback so that the existing preselection picks it for the
# computer side and the advice about installing a driver stays quiet.
SYSTEM_AUDIO = "system"

HELPER_SOURCE = Path(__file__).parent / "mac" / "syscapture.swift"
# Built once, into the data directory rather than scratch, which gets swept. A
# packaged .app ships this already built and signed and only ever finds it here.
HELPER_BIN = DATA_DIR / "syscapture"


async def helper_path() -> Path | None:
    """The system-audio helper, compiled on first use if this Mac can compile it.

    Nothing is built on Linux, where a PulseAudio monitor source already appears
    as an ordinary input and none of this is needed.
    """
    if sys.platform != "darwin":
        return None
    try:
        fresh = HELPER_BIN.stat().st_mtime >= HELPER_SOURCE.stat().st_mtime
    except OSError:
        # No source to compare against means a shipped binary: trust it if it runs.
        fresh = HELPER_BIN.is_file() and not HELPER_SOURCE.is_file()
    if fresh and os.access(HELPER_BIN, os.X_OK):
        return HELPER_BIN
    return await _build_helper()


async def _build_helper() -> Path | None:
    swiftc = shutil.which("swiftc")
    if swiftc is None or not HELPER_SOURCE.is_file():
        return None
    # Staged and moved, so a build interrupted halfway cannot leave a half-written
    # binary behind for the next run to trust.
    staged = HELPER_BIN.with_name("syscapture.building")
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        code, _ = await capture(
            # No -parse-as-library: the helper is top-level code, and under that
            # flag it compiles cleanly into a binary that does nothing whatever.
            [swiftc, "-O", "-o", str(staged), str(HELPER_SOURCE)],
            timeout=300)
    except (Failed, OSError):
        return None
    if code != 0 or not staged.is_file():
        staged.unlink(missing_ok=True)
        return None
    try:
        staged.replace(HELPER_BIN)
    except OSError:
        return None
    return HELPER_BIN


async def system_audio() -> dict | None:
    """The driverless computer-audio source, or None where there cannot be one.

    There is no permission to report. Core Audio offers no way to ask whether a
    process tap is allowed without creating one, and creating one succeeds whether
    or not the grant exists — an ungranted tap simply delivers silence. So macOS is
    asked at the moment of use, the way any other application asks, and a side that
    heard nothing is reported by the level check that already runs afterwards.
    """
    helper = await helper_path()
    return None if helper is None else {"helper": helper}


# --- what is available to record from ----------------------------------------


AVF_HEADER = re.compile(r"AVFoundation (audio|video) devices:")
AVF_PREFIX = re.compile(r"^\[AVFoundation[^\]]*\]\s*")
AVF_DEVICE = re.compile(r"^\[(\d+)\]\s+(.+?)\s*$")
PULSE_DEVICE = re.compile(r"^[*\s]+(\S+)\s+\[(.*)\]\s*$")


def _list_command() -> list[str]:
    if sys.platform == "darwin":
        return [binary("ffmpeg"), "-hide_banner", "-f", "avfoundation",
                "-list_devices", "true", "-i", ""]
    return [binary("ffmpeg"), "-hide_banner", "-sources", "pulse"]


def _parse_avfoundation(text: str) -> list[dict]:
    """The audio half of `-list_devices true`, which prints video first."""
    found, in_audio = [], False
    for raw in text.splitlines():
        line = AVF_PREFIX.sub("", raw).strip()
        header = AVF_HEADER.search(line)
        if header:
            in_audio = header.group(1) == "audio"
            continue
        if not in_audio:
            continue
        m = AVF_DEVICE.match(line)
        if m:
            found.append({"id": m.group(1), "name": m.group(2)})
    return found


def _parse_pulse(text: str) -> list[dict]:
    """`-sources pulse`, one indented `name [description]` per line."""
    found = []
    for raw in text.splitlines():
        m = PULSE_DEVICE.match(raw)
        if m:
            found.append({"id": m.group(1), "name": m.group(2) or m.group(1)})
    return found


async def _default_input_name() -> str:
    """The input macOS would pick by itself, which is the sensible first guess.

    Handy asks CoreAudio for its default input rather than taking the first device
    it is offered, and it is right to: "first in the list" put a Bluetooth headset
    on somebody's voice channel here, which recorded forty decibels of nothing.
    """
    if sys.platform != "darwin":
        return ""
    try:
        _, out = await capture(["system_profiler", "SPAudioDataType"], timeout=20)
    except (Failed, OSError):
        return ""
    name = ""
    for raw in out.splitlines():
        line = raw.strip()
        if line.endswith(":") and not line.startswith("Default"):
            name = line[:-1]
        elif "Default Input Device: Yes" in line:
            return name
    return ""


def resolve_saved(saved: str, found: list[dict]) -> str:
    """A remembered device, found again in today's list by the name it was saved as.

    By name, because avfoundation numbers devices by position and the position
    moves. A stored "1" meant the built-in microphone one day and a disconnected
    headset the next, and the recording that came back was silence that looked
    exactly like a bug in the recorder.
    """
    if not saved:
        return ""
    if saved == SYSTEM_AUDIO:
        return SYSTEM_AUDIO
    for device in found:
        if device["name"] == saved:
            return device["id"]
    # Saved before this was stored by name: a bare index, honoured only while some
    # device still sits at it.
    if saved.isdigit() and any(device["id"] == saved for device in found):
        return saved
    return ""


def name_for(device_id: str, found: list[dict]) -> str:
    """What to remember a chosen device as."""
    if device_id == SYSTEM_AUDIO:
        return SYSTEM_AUDIO
    for device in found:
        if device["id"] == device_id:
            return device["name"]
    return device_id


def is_loopback(device: dict) -> bool:
    haystack = f"{device['name']} {device['id']}".lower()
    return any(hint in haystack for hint in LOOPBACK_HINTS)


async def _core_audio_inputs() -> list[dict]:
    """The microphones, asked of Core Audio through the helper.

    Identified by UID rather than by position. ffmpeg numbers devices by where
    they sit in a list and that moves the moment anything is plugged in or taken
    away — a stored `1` has already meant two different microphones on this
    machine, and the recording that came back was silence that looked exactly like
    a bug in the recorder.
    """
    helper = await helper_path()
    if helper is None:
        return []
    try:
        code, out = await capture([str(helper), "--list-inputs"], timeout=20)
    except (Failed, OSError):
        return []
    if code != 0:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            found.append({"id": parts[0], "name": parts[1],
                          "default": len(parts) > 2 and parts[2].strip() == "default"})
    return found


async def devices() -> dict:
    """Audio inputs this machine offers, and what is missing if anything is.

    Never called from /api/state: listing devices spawns ffmpeg, and on macOS it
    opens each device briefly. Once per visit to the Record view is enough.
    """
    found = await _core_audio_inputs() if sys.platform == "darwin" else []
    if not found:
        # `-list_devices true` has no input to process, so ffmpeg prints the list
        # and then exits non-zero. The code says nothing about whether listing
        # worked. Still the answer everywhere but macOS, and the fallback there if
        # the helper cannot be built.
        _, out = await capture(_list_command(), timeout=30)
        found = _parse_avfoundation(out) if sys.platform == "darwin" else _parse_pulse(out)
    for device in found:
        device["loopback"] = is_loopback(device)
    # Listed last so that it is the computer side's first loopback without ever
    # being mistaken for the microphone, whose guess takes the first that is not.
    sysaudio = await system_audio()
    if sysaudio is not None:
        found.append({"id": SYSTEM_AUDIO, "name": "System audio",
                      "loopback": True, "builtin": True})
    if not any(device.get("default") for device in found):
        default_input = await _default_input_name()
        for device in found:
            device["default"] = bool(default_input) and device["name"] == default_input
    conf = recording_config()
    advice = []
    if not found:
        advice.append("noDevices")
    elif not any(d["loopback"] for d in found):
        advice.append("needLoopback")
    return {
        "devices": found,
        # Remembered by name, handed back as whatever index that name has today.
        "voice": resolve_saved(conf["voice"], found),
        "computer": resolve_saved(conf["computer"], found),
        "folder": conf["folder"],
        "labels": list(conf["labels"]),
        "advice": advice,
        # Only when there is nothing to show: otherwise this is ffmpeg noise about
        # devices that listed perfectly well.
        "log": [] if found else out.splitlines()[-20:],
    }


# --- recording ---------------------------------------------------------------


# Whatever a device hands over becomes one mono 48 kHz stream. Asking for a
# layout rather than a channel count is what makes this work against a 1-channel
# built-in mic, a 2-channel loopback and a 3-channel aggregate alike.
#
# aresample corrects the drift between two devices that were clocked separately.
# Applied to finished files, never to a live capture: given a live input it is free
# to insert silence to make the timestamps agree, and that is what shredded the
# microphone when both sources were mixed as they arrived.
FLAT = "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono"
ONE_STREAM = FLAT + ",aresample=async=1000:first_pts=0"


def pad_command(src: Path, dst: Path, gaps: list[tuple[float, float]]) -> list[str]:
    """Rewrite one capture with the silence it missed put back where it missed it.

    Each gap is (how much audio the capture had produced when it stalled, how long
    it stalled for). Appending the total at the end would give the right length and
    the wrong recording: the point is that a word said forty minutes in is forty
    minutes in, so the silence goes where the hole is.

    The file is opened once per segment rather than split inside the graph. asplit
    would make the other branches wait, buffered in memory, while concat reads the
    first one to the end — which on a three-hour recording is gigabytes of it.
    Reading a local WAV a second time costs nothing worth counting.
    """
    parts, labels, at = [], [], 0.0
    for n, (start, length) in enumerate(gaps):
        start = max(start, at)
        parts.append(f"[{n}:a]atrim=start={at}:end={start},{FLAT},asetpts=N/SR/TB[k{n}]")
        parts.append(f"anullsrc=r=48000:cl=mono,atrim=duration={length},{FLAT}[g{n}]")
        labels += [f"[k{n}]", f"[g{n}]"]
        at = start
    parts.append(f"[{len(gaps)}:a]atrim=start={at},{FLAT},asetpts=N/SR/TB[tail]")
    labels.append("[tail]")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
    cmd = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]
    for _ in range(len(gaps) + 1):
        cmd += ["-i", str(src)]
    return cmd + ["-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", str(dst)]


# Fill the holes the device leaves, using the timestamps it already provides.
#
# Measured, and it is not small: the microphone was handing over 17.05 seconds of
# audio for every 20 seconds of wall clock. Not a startup gap — the shortfall grows
# with the recording, and the ratio held at about 0.78 across runs of 10, 30 and 60
# seconds. Letting ffmpeg stop itself at 20 seconds of stream time is what named it:
# it took 20.36 seconds of clock to get there, so the device's timestamps are right
# and it is the samples between them that never arrive. WAV carries no timestamps,
# so the holes close up and the recording plays back short.
#
# Confirmed from the other end, with a pattern played through the speakers exactly
# two seconds apart: the tap recorded it two seconds apart, the microphone recorded
# it 1.775 seconds apart. That is nearly seven minutes of drift in an hour between
# the two sides of the same conversation.
#
# async=1 fills and trims only, and never stretches. It is not the setting that once
# ruined the quieter side of a recording — that was async=1000 reconciling two live
# devices inside one ffmpeg, which is a different job this no longer asks of it.
KEEP_TIME = "aresample=async=1"

# Two meters from one pass, because they answer different questions.
#
# ebur128 says how loud a thing is the way a broadcaster means it, and everything
# that judges a recording is built on it. It cannot show a voice moving, though,
# and that is not a matter of asking it more often: momentary loudness is defined
# over a 400 ms window, and 400 ms is about two syllables. Measured on a tone
# switching on and off every 200 ms — the rhythm of ordinary speech — it reported
# a flat -24.5 dB throughout, never once dipping, because its window spans exactly
# one on and one off and averages them away. A meter fed that number is not slow,
# it is showing the wrong quantity.
#
# So the needle is driven by peak instead, over 50 ms windows, which on the same
# tone alternated cleanly between -18.1 and silence. asetnsamples fixes the window
# rather than inheriting whatever frame size the device hands over, so the meter
# moves at the same rate on every machine. The tap needs none of this: the helper
# has always reported peak, and only reported it too rarely.
METERS = ("ebur128=metadata=1,ametadata=print:key=lavfi.r128.M,"
          "asetnsamples=n=2400:p=0,astats=metadata=1:reset=1,"
          "ametadata=print:key=lavfi.astats.Overall.Peak_level")


def capture_command(rec: dict, device: str, out: Path) -> list[str] | None:
    """ffmpeg's part of a recording: the microphone, alone, to its own file.

    Alone on purpose. Both sources used to arrive as two live inputs of one ffmpeg,
    joined as they came, and that meant reconciling two independent clocks in real
    time. aresample filled the difference with silence — measured at 0.237 s of it
    nearly four times a second — which left the louder side intact and destroyed
    the quieter one, so a voice came back in pieces too broken for VAD to consider
    speech. The same microphone recorded on its own is clean. Nothing is mixed
    until both captures have finished and there is nothing left to reconcile.
    """
    if not device:
        return None
    source = ["-f", "avfoundation", "-i", f":{device}"] if sys.platform == "darwin" \
        else ["-f", "pulse", "-i", device]
    # Nothing is asked of the device beyond what it offers. Demanding a rate or a
    # layout of a live capture is resampling in the capture path, and the capture
    # path is the one place that cannot afford to fall behind.
    return ([binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "info", "-y",
             "-thread_queue_size", "1024"] + source
            # An output that keeps nothing and exists to report how loud the input
            # is. The interface had a bar that swept on a timer whatever the audio
            # was doing, which is how a microphone recording digital zero went
            # unnoticed for hours; a meter has to measure something or it is
            # decoration that lies. First, so that the recording stays the last
            # thing on the line and reads as the point of the command.
            #
            # Both outputs are kept honest the same way, and they have to be the
            # same way: the meter is what tells the app how much audio has arrived,
            # so a meter measuring one timeline and a file holding another is how
            # the app would come to put a gap back that ffmpeg had already filled.
            + ["-t", str(rec["max_seconds"]), "-af", f"{KEEP_TIME},{METERS}",
               "-f", "null", "-"]
            + ["-t", str(rec["max_seconds"]), "-af", KEEP_TIME,
               "-c:a", "pcm_s16le", str(out)])


def capture_commands(rec: dict) -> list[list[str]]:
    """One ffmpeg per real device. The driverless source is the helper's job.

    One process each rather than one process with two inputs, which is the whole
    change: a single ffmpeg had to reconcile two clocks as the audio arrived, and
    filled the difference with silence.
    """
    out = []
    for side, device, path in (("voice", rec["voice"], rec["voice_wav"]),
                               ("computer", rec["computer"], rec["computer_wav"])):
        if not device or device == SYSTEM_AUDIO:
            continue
        # The microphone belongs to the helper wherever there is one. ffmpeg's
        # avfoundation input was handing over 86% of the samples the device
        # produced; Core Audio hands over all of them. What is left for ffmpeg
        # here is a real loopback device chosen as the computer's side, and
        # everywhere that is not macOS.
        if side == "voice" and helper_takes_the_microphone(rec):
            continue
        out.append(capture_command(rec, device, path))
    return out


def helper_takes_the_microphone(rec: dict) -> bool:
    """Whether the microphone is the helper's job rather than ffmpeg's."""
    return bool(rec.get("helper")) and bool(rec.get("voice")) \
        and rec.get("voice") != SYSTEM_AUDIO


def _captured_bytes(rec: dict) -> int:
    """How much audio has arrived, across whichever sources are running."""
    total = 0
    for key in ("voice_wav", "voice_pcm", "computer_wav", "sys_pcm"):
        path = rec.get(key)
        try:
            total += path.stat().st_size
        except (OSError, AttributeError):
            pass
    return total


def captured_sources(rec: dict) -> list[str]:
    """Which sides actually recorded something, in channel order: voice, then computer.

    Size alone stopped being the answer when the tap started writing down its own
    silences. It now produces a full-length file on a machine that played nothing
    at all, and keeping that would mean a stereo master with a dead right channel
    and a transcription run over an hour of digital zero. So the tap is asked the
    other question as well: did any sound ever reach it. `ever` is missing entirely
    on a recording rescued from a crash, and unknown is not the same as no.
    """
    out = []
    for keys, label in ((("voice_wav", "voice_pcm"), "voice"),
                        (("computer_wav", "sys_pcm"), "computer")):
        heard = rec.get("ever")
        if label == "computer" and rec.get("computer") == SYSTEM_AUDIO \
                and heard is not None and label not in heard:
            continue
        for key in keys:
            path = rec.get(key)
            try:
                if path is not None and path.stat().st_size > EMPTY_WAV:
                    out.append(label)
                    break
            except OSError:
                continue
    return out


def raw_input_for(rec: dict, wav_key: str, pcm_key: str) -> list[str]:
    """One input for a side, from whichever of its two files it actually used.

    A side is captured either by ffmpeg into a WAV or by the helper into raw
    samples, never both. Raw samples carry no header, so the format the helper
    promised has to be stated on the command line.
    """
    wav, pcm = rec.get(wav_key), rec.get(pcm_key)
    try:
        if wav is not None and wav.is_file() and wav.stat().st_size > EMPTY_WAV:
            return ["-i", str(wav)]
    except OSError:
        pass
    # A recording rescued from a crash predates knowing about the second file, so
    # the WAV is the only answer there is.
    if pcm is None:
        return ["-i", str(wav)]
    return ["-f", "s16le", "-ar", "48000", "-ac", "1", "-i", str(pcm)]


def mix_command(rec: dict, sources: list[str]) -> list[str]:
    """Combine the finished captures into the stereo master that gets kept.

    Offline, from complete files, which is the whole point: there are no clocks
    left to chase, so aresample corrects the drift between the two once instead of
    guessing at it thousands of times while recording.
    """
    cmd = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]
    if "voice" in sources:
        cmd += raw_input_for(rec, "voice_wav", "voice_pcm")
    if "computer" in sources:
        cmd += raw_input_for(rec, "computer_wav", "sys_pcm")
    if len(sources) == 2:
        graph = (f"[0:a]{ONE_STREAM}[voice];[1:a]{ONE_STREAM}[computer];"
                 "[voice][computer]join=inputs=2:channel_layout=stereo[out]")
    else:
        graph = f"[0:a]{ONE_STREAM}[out]"
    return cmd + ["-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le",
                  str(rec["wav"])]


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H.%M")


def _unique(path: Path) -> Path:
    """Never overwrite a recording. Two in the same minute get -2, -3, …"""
    if not path.exists():
        return path
    for n in range(2, 500):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}")


async def start(voice: str, computer: str) -> dict:
    """Begin recording. Returns once audio is actually arriving, or explains why not."""
    global RECORDING, TASK
    if RECORDING is not None and RECORDING["status"] in LIVE:
        raise Failed("already_recording", "A recording is already running.")

    chosen = [d for d in (voice.strip(), computer.strip()) if d]
    if not chosen:
        raise Failed("invalid_input_path", "Choose at least one thing to record.")

    helper = None
    # The microphone goes through the helper too now, so it is wanted on macOS
    # whenever anything at all is being recorded — not only for the tap.
    if sys.platform == "darwin" and voice.strip() and SYSTEM_AUDIO not in chosen:
        sysaudio = await system_audio()
        helper = sysaudio["helper"] if sysaudio is not None else None
    if SYSTEM_AUDIO in chosen:
        # macOS is asked by the click that starts the recording, not silently at
        # startup: a permission prompt out of nowhere is worse than one with a
        # reason. There is nothing to check beforehand — a process tap is created
        # whether or not it is allowed, and an unallowed one just returns silence.
        sysaudio = await system_audio()
        if sysaudio is None:
            raise Failed("dependency_not_found",
                         "The system-audio helper could not be built. Xcode's command line "
                         "tools provide the compiler it needs (xcode-select --install).")
        helper = sysaudio["helper"]

    conf = recording_config()
    folder = Path(conf["folder"]).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Failed("insufficient_permissions",
                     f"The recordings folder {folder} could not be made "
                     f"({exc.strerror or exc}). Choose another one in Settings.")
    if not os.access(folder, os.W_OK):
        raise Failed("insufficient_permissions", f"Recordings cannot be written to {folder}. Choose another folder in Settings.")

    # The master WAV costs about 700 MB an hour, so a nearly full disk is worth
    # saying out loud before an hour of a meeting goes missing.
    try:
        free_gb = shutil.disk_usage(WORK_DIR).free / 1e9
        if free_gb < 1:
            raise Failed("insufficient_permissions",
                         f"Only {free_gb:.1f} GB of disk is free. Recording needs about "
                         "0.7 GB per hour while it runs.")
    except OSError:
        pass

    rec_id = uuid.uuid4().hex[:12]
    work = WORK_DIR / f"{RECORDING_PREFIX}{rec_id}"
    work.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": rec_id,
        "status": "recording",
        "devices": chosen,
        "voice": voice.strip(),
        "computer": computer.strip(),
        "labels": list(conf["labels"]),
        "folder": str(folder),
        "basename": _stamp(),
        "max_seconds": conf["max_seconds"],
        "transcribe": conf["transcribe"],
        "keep": True,
        "started_at": time.time(),
        "ended_at": None,
        "path": None,
        "job_id": None,
        "error": None,
        "work": work,
        # Made at the end, out of the two beside it. Nothing writes to it while a
        # recording is running.
        "wav": work / "master.wav",
        "helper": helper,
        # Each capture owns a file and nothing else writes to it. A plain file
        # rather than the FIFO this used to be: there is no reader to hand off to
        # any more, so there is no handshake to get wrong either.
        "voice_wav": work / "voice.wav",
        "computer_wav": work / "computer.wav",
        "sys_pcm": work / "computer.pcm",
        # The microphone, when the helper is taking it. Raw for the same reasons
        # the tap is: ffmpeg is told the format on its command line, and every
        # prefix of a raw stream is still audio.
        "voice_pcm": work / "voice.pcm",
        "log": deque(maxlen=MAX_LOG),
        # Sides that real audio ever reached, as against sides whose file merely
        # exists. Present from the start so that empty means no rather than unknown.
        "ever": set(),
        # Time deliberately not recorded, so the clock can agree with the file.
        "paused_at": None,
        "paused_total": 0.0,
    }
    RECORDING = rec
    _checkpoint(rec)
    TASK = asyncio.create_task(_run(rec))
    await _until_audio_arrives(rec)
    if rec["status"] == "failed":
        raise Failed(rec["error"]["code"], rec["error"]["message"])
    # A side that is not arriving is said on the recording screen and left there,
    # rather than ending the recording. Killing it was too strong: a recording that
    # has nothing to hear yet is a perfectly ordinary thing — somebody presses
    # record before the meeting starts — and being thrown out for it is worse than
    # being told. The warning clears itself the moment audio turns up.
    # Remember the choice by name, so the next recording is one decision lighter and
    # still points at the same device after something is plugged in or unplugged.
    known = await _core_audio_inputs() if sys.platform == "darwin" else []
    if not known:
        try:
            _, listing = await capture(_list_command(), timeout=30)
            known = _parse_avfoundation(listing) if sys.platform == "darwin" \
                else _parse_pulse(listing)
        except (Failed, OSError):
            known = []
    # Only what was actually chosen. A device that could not be seen at this moment
    # must not erase the one that was remembered — clearing the microphone grant
    # emptied the device listing, which wrote an empty choice over a good one, and
    # every recording afterwards was the computer's side alone with no word said.
    remember = {}
    for key, side in (("record_voice_device", "voice"),
                      ("record_computer_device", "computer")):
        name = name_for(rec[side], known)
        if name:
            remember[key] = name
    if remember:
        save_settings(remember)
    return public()


def _side_bytes(rec: dict, side: str) -> int:
    """How much has arrived on one side, whichever file that side is writing to."""
    keys = ("voice_wav", "voice_pcm") if side == "voice" else ("computer_wav", "sys_pcm")
    total = 0
    for key in keys:
        try:
            total += rec[key].stat().st_size
        except (OSError, AttributeError, KeyError):
            pass
    return total


def _side_arriving(rec: dict, side: str) -> bool:
    """Whether a side is actually producing audio, by the soonest honest signal.

    The meter first, because it is immediate: ffmpeg reports loudness for every
    frame that arrives and reports nothing at all when none do, so it separates a
    working capture from a refused one within a fraction of a second.

    The file size cannot do that job. ffmpeg buffers its output, and a microphone
    working perfectly well leaves its WAV at zero bytes on disk for tens of seconds
    before the first flush — which is exactly how a recording that was working was
    refused for not working, with the meter sitting there the whole time reading
    -43 dB.

    The computer's side, when it is the tap, cannot be asked this question at all.
    A Core Audio tap on an output device playing nothing delivers no callbacks
    whatever — measured, 0 bytes with the machine quiet against 285,696 with a
    sound playing — so an empty file means the room was quiet, not that the capture
    is broken. Reading it as broken is how somebody came to be told their computer's
    audio was not being captured while it worked perfectly and simply had nothing to
    capture. All that can honestly be asked of it is whether the helper is running.
    """
    if side == "computer" and rec.get("computer") == SYSTEM_AUDIO:
        # Only whether the helper is still there. Frames of digital zero are not
        # proof of a refusal after all: when a sound stops, the output device keeps
        # running for a moment and hands over exactly that — so the tail of every
        # piece of audio looks like being refused. It was measured doing it, at the
        # end of this check's own tone.
        #
        # Digital zero only means something when something is known to be playing,
        # and the one place that is known is the check, which plays the sound
        # itself. See check_verdict. Here, the honest question is the smaller one.
        return rec.get("helper_code") is None
    if rec.get("live", {}).get(side) is not None:
        return True
    return _side_bytes(rec, side) > EMPTY_WAV


def _asked_for(rec: dict) -> list[str]:
    return [side for side, chosen in (("voice", rec.get("voice")),
                                      ("computer", rec.get("computer"))) if chosen]


async def _until_audio_arrives(rec: dict, timeout: float = 6.0) -> list[str]:
    """Wait for every side that was asked for to start growing. Returns the ones
    that never did.

    Each side on its own, which is the whole point. This used to add the sides
    together and stop as soon as the total moved, so a working microphone answered
    for a computer channel that was producing nothing at all — and the recording ran
    for forty seconds looking perfectly healthy while capturing half of what it had
    been asked for. An ungranted audio tap writes no bytes whatever, so this is not
    a guess: it is the difference between a source that is working and one that is
    not, available within a second of pressing the button.
    """
    wanted = _asked_for(rec)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rec["status"] != "recording":
            return []
        missing = [side for side in wanted if not _side_arriving(rec, side)]
        if not missing:
            return []
        await asyncio.sleep(0.1)
    return [side for side in wanted if not _side_arriving(rec, side)]


async def _start_helper(rec: dict) -> bool:
    """Begin capturing the computer's audio into the FIFO ffmpeg is about to read.

    It writes to a file of its own, so it neither waits for ffmpeg nor races it.
    """
    global HELPER
    cmd = [str(rec["helper"])]
    if rec.get("computer") == SYSTEM_AUDIO:
        cmd += ["--tap", str(rec["sys_pcm"])]
    if helper_takes_the_microphone(rec):
        # By UID. Positions move when anything is plugged in; a stored index has
        # already meant two different microphones on this machine.
        cmd += ["--mic", str(rec["voice_pcm"]), "--mic-device", rec["voice"]]
    rec["log"].append("$ " + shlex.join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        _failed(rec, "recording_failed", f"The system-audio helper would not start: {exc}")
        return False
    HELPER = proc
    asyncio.create_task(_drain_helper(rec, proc))
    return True


async def _drain_helper(rec: dict, proc: asyncio.subprocess.Process) -> None:
    """The helper's own words, and the exit code that says whether it was allowed."""
    global HELPER
    try:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            found = HELPER_LEVEL.search(line)
            if found:
                # Kept out of the log, like ffmpeg's: ten lines a second would
                # bury everything worth reading.
                side, level = found.group(1), float(found.group(2))
                rec.setdefault("live", {})[side] = level
                # The helper only speaks when frames actually arrived, so this is
                # the one honest record that sound ever reached it — its files now
                # fill themselves with silence either way. See captured_sources.
                rec.setdefault("peak", {})[side] = level
                if isinstance(rec.get("ever"), set):
                    rec["ever"].add(side)
                continue
            rec["log"].append(line)
        await proc.wait()
    finally:
        if HELPER is proc:
            HELPER = None
        rec["helper_code"] = proc.returncode
        for side in ("voice", "computer"):
            rec.setdefault("live", {}).pop(side, None)
            rec.setdefault("peak", {}).pop(side, None)


async def _until_mic_ready(rec: dict, timeout: float = 4.0) -> None:
    """Hold the tap back until the microphone is actually delivering.

    Order is not a detail here, it is the whole thing. Creating the aggregate
    device that carries a Core Audio process tap reconfigures the audio HAL, and an
    AVFoundation capture session opened after that never delivers a single sample —
    the device opens, ffmpeg prints no start timestamp, and not one frame arrives.
    Measured outside the app, twice, both ways round: microphone first and it keeps
    running while the tap is created; tap first and the microphone yields zero
    frames for as long as you care to wait.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rec["status"] != "recording" or _side_arriving(rec, "voice"):
            return
        await asyncio.sleep(0.05)


async def _run(rec: dict) -> None:
    """One recording, start to finish: capture, then save what was captured."""
    global PROC
    commands = capture_commands(rec)
    if not commands:
        # The computer's audio and nothing else: the helper is the whole capture,
        # so there is no ffmpeg to wait on and nothing to be disturbed by the tap.
        if rec.get("helper") is not None and not await _start_helper(rec):
            return
        asyncio.create_task(_watch_disk(rec))
        await _await_helper(rec)
        return await _finish(rec)
    procs = []
    for cmd in commands:
        rec["log"].append("$ " + shlex.join(cmd))
        try:
            procs.append(await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            ))
        except OSError as exc:
            for started in procs:
                _signal_proc(started, signal.SIGKILL)
            return _failed(rec, "ffmpeg_failed", f"ffmpeg would not start: {exc}")
    PROC = procs[0]
    PROCS.clear()
    PROCS.extend(procs)
    # Only now, and only once the microphone is live. See _until_mic_ready.
    if rec.get("helper") is not None:
        # Only when ffmpeg holds the microphone. When the helper holds it there is
        # nothing to wait for: it starts the microphone before the tap inside its
        # own process, which is the whole reason for putting them together.
        if not helper_takes_the_microphone(rec):
            await _until_mic_ready(rec)
        if not await _start_helper(rec):
            return
    asyncio.create_task(_watch_disk(rec))
    try:
        # Labelled in the order capture_commands built them: the voice first, then a
        # real device on the computer's side if one was chosen.
        labels = [side for side, device in (("voice", rec["voice"]), ("computer", rec["computer"]))
                  if device and device != SYSTEM_AUDIO]
        await asyncio.gather(*(_drain(rec, proc, label)
                               for proc, label in zip(procs, labels)))
        # A capture that ends before anybody asked it to has failed, not finished.
        # Ending the recording along with it would throw away the source that is
        # still working — a Bluetooth microphone dropping out two minutes into a
        # meeting used to take the computer's audio down with it. Whatever survives
        # keeps recording until the recording is stopped or runs out of time.
        if rec["status"] == "recording" and HELPER is not None and HELPER.returncode is None:
            for proc in procs:
                if proc.returncode not in (0, None):
                    rec["log"].append(
                        f"# a capture exited by itself with {proc.returncode}; "
                        "the rest of the recording carries on")
            await _await_helper(rec)
    finally:
        PROC = None
        PROCS.clear()
        # The captures are over, so the helper has nobody left to keep pace with.
        _signal_helper(signal.SIGINT)

    await _finish(rec)


async def _watch_disk(rec: dict, poll: float = 20.0) -> None:
    """Stop the recording before the disk fills rather than after.

    Space is checked once before a recording starts, which says nothing about an
    hour later. A capture that runs out of room mid-meeting loses the end of it and
    leaves the machine with nothing free either; stopping while there is still room
    keeps everything up to that point and says why in the log.
    """
    while rec["status"] in ("recording", "paused"):
        try:
            free = shutil.disk_usage(WORK_DIR).free
        except OSError:
            return  # unreadable is not a reason to end a recording
        if disk_is_low(free):
            rec["log"].append(
                f"# only {free / 1e9:.1f} GB of disk left, so the recording was stopped "
                "early and saved")
            rec["low_disk"] = True
            rec["status"] = "stopping"
            _signal(signal.SIGINT)
            return
        await asyncio.sleep(poll)


async def _drain(rec: dict, proc: asyncio.subprocess.Process, label: str = "voice") -> None:
    """One capture's own words, and how loud it is while it says them."""
    assert proc.stderr is not None
    async for raw in proc.stderr:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        found = EBUR128_M.search(line)
        if found:
            # Kept out of the log: this arrives ten times a second and would bury
            # everything worth reading.
            try:
                rec.setdefault("live", {})[label] = float(found.group(1))
            except ValueError:
                pass
            _heard(rec, label)
            continue
        found = PEAK.search(line)
        if found:
            raw = found.group(1)
            rec.setdefault("peak", {})[label] = (
                DIGITAL_SILENCE if raw.endswith("inf") else float(raw))
            continue
        if "Parsed_ametadata" in line:
            # The frame/pts line that comes with every measurement. It was going
            # into the log, ten lines a second, and the log holds 120 — so it held
            # twelve seconds of nothing and every message ffmpeg had for us was
            # pushed out of it long before anybody looked.
            continue
        rec["log"].append(line)
    await proc.wait()
    rec.setdefault("live", {}).pop(label, None)
    rec.setdefault("peak", {}).pop(label, None)
    rec.setdefault("moved", {}).pop(label, None)


# ebur128 reports momentary loudness once for every 100 ms of audio, so each of
# those lines is a tenth of a second the capture genuinely received.
HEARD_PER_LINE = 0.1

# How long a capture may hand over nothing before that is a stall rather than the
# ordinary jitter of a pipe. Loudness is reported whether or not anybody is
# speaking, so a pause in a conversation does not stop these lines — only the
# capture stopping does.
STALL = 2.0


def _heard(rec: dict, side: str) -> None:
    """A tenth of a second more audio, and a note if the wall clock ran on without it.

    This is how a stall is caught. A recording left open across a sleep came back
    31 seconds short of the 70 seconds it was open for: both captures kept their
    process ids, both stopped producing and then started again unasked, and the
    status never left `recording`. The audio simply stopped arriving, and the hole
    closed up rather than staying open — which does not make a shorter recording,
    it makes a wrong one, because everything said afterwards then carries a
    timestamp 31 seconds earlier than the moment it was said.

    The gap is measured against the wall clock rather than against ffmpeg's own
    timestamps, because a device whose clock stopped along with it would report no
    time having passed at all.
    """
    now = time.monotonic()
    heard = rec.setdefault("heard", {}).get(side, 0.0)
    last = rec.setdefault("moved", {}).get(side)
    if last is not None:
        missed = (now - last) - HEARD_PER_LINE
        if missed >= STALL:
            rec.setdefault("gaps", {}).setdefault(side, []).append(
                (round(heard, 3), round(missed, 3)))
            rec["log"].append(
                f"# the {side} capture handed over nothing for {missed:.1f}s; that "
                "silence goes back in before the recording is saved")
    rec["moved"][side] = now
    rec["heard"][side] = heard + HEARD_PER_LINE


def stalled_sides(rec: dict) -> list[str]:
    """Sides that were arriving and have stopped.

    A different question from `not_arriving`, which asks whether a side ever
    started. The live warning could only ever ask the first one, and it read the
    last level it was handed — a level that stopped being updated sits there
    looking perfectly healthy. So a recording that had gone deaf half a minute ago
    showed a moving meter and said nothing at all.
    """
    now = time.monotonic()
    return [side for side, when in (rec.get("moved") or {}).items() if now - when >= STALL]


async def _await_helper(rec: dict, poll: float = 0.2) -> None:
    """Wait out a recording that only the helper is making."""
    # Measured from when the recording began, so a source lost halfway does not
    # quietly grant the rest another full allowance.
    deadline = rec["started_at"] + rec["max_seconds"]
    while time.time() < deadline:
        if HELPER is None or HELPER.returncode is not None:
            return
        if rec["status"] not in ("recording", "paused", "stopping"):
            break
        await asyncio.sleep(poll)
    # Forgotten about, or asked to stop: either way the capture ends here.
    _signal_helper(signal.SIGINT)


async def _finish(rec: dict) -> None:
    """Both captures are over. Combine them, then save what they caught."""
    if not rec["keep"]:
        rec["status"] = "discarded"
        rec["ended_at"] = time.time()
        shutil.rmtree(rec["work"], ignore_errors=True)
        return
    # Which sides recorded, rather than which were asked for. A source that was
    # selected and then produced nothing is not a channel worth keeping, and
    # labelling silence as a speaker is how an empty track reached a transcript.
    sources = captured_sources(rec)
    rec["sources"] = sources
    if not sources:
        return _failed(rec, *_why_nothing_arrived(rec))
    await _pad_gaps(rec)
    if not await _mix(rec, sources):
        return
    # Reached even when a capture died of its own accord: whatever arrived before
    # it stopped is still a recording, and still worth keeping.
    await _save(rec)


# How long a capture is allowed to take to produce its first audio before the
# recording screen says out loud that it is producing none. Long enough to cover an
# ordinary start, short enough that nobody talks for a minute into nothing.
SETTLING = 4.0

# A channel quieter than this carried nothing worth transcribing. Chosen well below
# a quiet voice — a whisper at arm's length measures around -40 — so that only
# genuine silence or a dead device trips it.
SILENT_DB = -60.0


async def channel_levels(path: Path, sources: list[str]) -> dict[str, float | None]:
    """The loudest sample in each side of a finished recording.

    Measured because nothing else can tell the difference. A recording made from a
    device that was asleep, or muted, or simply the wrong one, is exactly the same
    size as a good one and reports exactly the same success. Several hours went into
    a transcription problem that was a microphone recording digital zero, and this
    is the check that would have said so in a second.
    """
    out: dict[str, float | None] = {}
    for index, label in enumerate(sources):
        pan = f"c{index}" if len(sources) > 1 else "c0"
        peak = None
        try:
            _, text = await capture(
                [binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
                 "-af", f"pan=mono|c0={pan},volumedetect", "-f", "null", "-"],
                timeout=600)
            for line in text.splitlines():
                if "max_volume:" in line:
                    peak = float(line.split("max_volume:")[1].strip().split()[0])
        except (Failed, OSError, ValueError, IndexError):
            peak = None
        out[label] = peak
    return out


def quiet_sides(levels: dict[str, float | None]) -> list[str]:
    """Which sides came back with nothing audible in them."""
    return [label for label, peak in levels.items()
            if peak is not None and peak <= SILENT_DB]


def silent_sides(rec: dict, sources: list[str], levels: dict[str, float | None]) -> list[str]:
    """Every side worth saying something about: captured but silent, or never there.

    A side that was asked for and produced nothing at all is not merely quiet, it is
    absent — and it drops out of `sources` before anything measures it, so the levels
    never see it and nothing anywhere mentions it. What came back was a mono
    recording where two channels were asked for, with no word said. Which is exactly
    how a cleared permission looked from the outside: a meeting recorded, half of it
    missing, and the first sign of trouble a transcript with half a conversation in it.
    """
    quiet = quiet_sides(levels)
    # .get, because a recording rescued from a crash is rebuilt from its checkpoint
    # and never knew which sources were asked for. Nothing extra is claimed about
    # one of those, which is right: nobody can say what it was supposed to contain.
    for side, chosen in (("voice", rec.get("voice")), ("computer", rec.get("computer"))):
        if chosen and side not in sources and side not in quiet:
            quiet.append(side)
    return quiet


async def _pad_gaps(rec: dict) -> None:
    """Put the missed silence back into every capture that stalled.

    Never fatal. A track with a hole in it is worse than one without and far better
    than no recording at all — the audio is all there either way, and only the
    times it is laid out against are wrong.
    """
    for side, gaps in (rec.get("gaps") or {}).items():
        key = f"{side}_wav"
        src = rec.get(key)
        if not gaps or src is None or not src.is_file():
            continue
        dst = src.with_name(f"{side}-whole.wav")
        cmd = pad_command(src, dst, gaps)
        rec["log"].append("$ " + shlex.join(cmd))
        try:
            code, out = await capture(cmd, timeout=1800)
        except (Failed, OSError) as exc:
            rec["log"].append(f"# the {side} track kept its gaps: {exc}")
            continue
        if code != 0 or not dst.is_file():
            rec["log"] += out.splitlines()[-5:]
            rec["log"].append(f"# the {side} track could not have its silence put back, "
                              "so it is kept exactly as it was recorded")
            continue
        rec[key] = dst
        rec["log"].append(
            f"# put {sum(length for _, length in gaps):.1f}s of missed silence back into "
            f"the {side} track, so the times in the transcript still line up")


async def _mix(rec: dict, sources: list[str]) -> bool:
    cmd = mix_command(rec, sources)
    rec["log"].append("$ " + shlex.join(cmd))
    try:
        code, out = await capture(cmd, timeout=1800)
    except Failed as exc:
        _failed(rec, exc.code, exc.message)
        return False
    if code != 0 or not rec["wav"].is_file():
        rec["log"] += out.splitlines()[-10:]
        _failed(rec, "ffmpeg_failed", "The recorded audio could not be combined into one file.")
        return False
    return True


def _why_nothing_arrived(rec: dict) -> tuple[str, str]:
    text = " ".join(rec["log"]).lower()
    # The helper reports a refused permission as its exit code, so this is known
    # rather than guessed from log text. Checked first: when the computer's audio
    # was never allowed, nothing else that went wrong afterwards is the cause.
    if rec.get("helper_code") == HELPER_DENIED:
        return ("insufficient_permissions",
                "macOS did not let this app capture the computer's audio. Open System "
                "Settings → Privacy & Security → System Audio Recording Only, allow it "
                "there, then start the app again and record.")
    denied = ("not permitted", "input/output error", "permission denied",
              "cannot open", "no such device", "invalid device")
    if sys.platform == "darwin" and any(hint in text for hint in denied):
        return ("insufficient_permissions",
                "macOS did not let this app use the microphone. Open System Settings → "
                "Privacy & Security → Microphone, allow it there, and record again.")
    if len(rec["devices"]) == 2 and SYSTEM_AUDIO not in rec["devices"]:
        # Two devices means two capture sessions at once, and a machine that will
        # not open both leaves one way out worth naming: build an Aggregate Device
        # in Audio MIDI Setup holding both, and record that as a single source.
        # The channels come back mixed, so the transcript loses its speaker
        # labels — but it is a recording rather than nothing.
        return ("recording_failed",
                "ffmpeg recorded no audio from the two devices together. Try one of them on "
                "its own; or combine both into one Aggregate Device in Audio MIDI Setup and "
                "record that as a single source, which works but cannot label speakers.")
    if rec.get("helper") is not None:
        return ("recording_failed",
                "Nothing was captured. If the computer was playing nothing and no microphone "
                "was chosen, there was nothing to record — pick a microphone and try again.")
    return ("recording_failed", "ffmpeg stopped without recording any audio. "
                                "The process log below says what it reported.")


async def _save(rec: dict) -> None:
    """Turn the scratch WAV into the .m4a that gets kept, and queue it."""
    rec["status"] = "saving"
    _checkpoint(rec)
    # Measured while the master still exists, and never fatal: a recording with one
    # silent side is still a recording and still worth keeping. It is said out loud
    # rather than left to be discovered in a transcript that is missing half a
    # conversation.
    sources = rec.get("sources") or ["voice", "computer"][:len(rec["devices"])]
    rec["levels"] = await channel_levels(rec["wav"], sources)
    rec["quiet"] = silent_sides(rec, sources, rec["levels"])
    if rec["quiet"]:
        rec["log"].append("# nothing audible on: " + ", ".join(rec["quiet"]))
    stereo = len(rec.get("sources") or rec["devices"]) == 2
    staged = rec["work"] / "recording.m4a"
    cmd = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-i", str(rec["wav"]), "-c:a", "aac", "-b:a", "160k" if stereo else "96k",
           str(staged)]
    rec["log"].append("$ " + shlex.join(cmd))
    try:
        code, out = await capture(cmd, timeout=900)
    except Failed as exc:
        return _failed(rec, exc.code, exc.message)
    if code != 0 or not staged.exists():
        rec["log"] += out.splitlines()[-10:]
        return _failed(rec, "ffmpeg_failed", "The recording could not be saved as an .m4a.")

    final = _unique(Path(rec["folder"]) / f"{rec['basename']}.m4a")
    try:
        # Off the loop: the recordings folder can be anywhere, including the folders
        # macOS guards, and a move into one of those blocks until a consent dialog
        # is answered. On the loop that would take the whole app down with it, in
        # the seconds right after somebody pressed Stop.
        await asyncio.to_thread(shutil.move, str(staged), str(final))
    except OSError as exc:
        return _failed(rec, "insufficient_permissions",
                       f"The recording could not be moved to {final}: {exc.strerror or exc}")
    rec["path"] = str(final)
    rec["status"] = "saved"
    rec["ended_at"] = time.time()
    if rec["transcribe"]:
        await enqueue(rec, final)
    # The WAV has served its purpose; the .m4a is out of scratch and safe.
    shutil.rmtree(rec["work"], ignore_errors=True)


async def enqueue(rec: dict, path: Path) -> str | None:
    """Queue the recording for transcription, one track per source."""
    conf = settings()
    model = conf.get("default_model_path", "")
    if not model or not Path(model).is_file():
        rec["log"].append("# no model chosen yet, so the recording was not queued")
        return None
    sources = rec.get("sources") or ["voice", "computer"][:len(rec["devices"])]
    if len(sources) == 2:
        # Left is the voice, right is the machine — the order mix_command used.
        tracks = [{"channel": 0, "label": rec["labels"][0]},
                  {"channel": 1, "label": rec["labels"][1]}]
    else:
        # One side only, so there is nobody to tell apart and no label to carry.
        tracks = [{"channel": None, "label": ""}]
    job = jobs.make_job(
        str(path), model, watch.output_folder_for(path), f"{path.stem}{TRANSCRIPT_SUFFIX}",
        language=conf.get("default_language", "he"),
        extra_args=conf.get("default_extra_args") or DEFAULT_EXTRA,
        vad_model=conf.get("vad_model_path", ""),
        vocabulary=conf.get("vocabulary", ""),
        duration=await duration_seconds(path),
        tracks=tracks,
    )
    jobs.enqueue(job)
    rec["job_id"] = job["id"]
    return job["id"]


def _failed(rec: dict, code: str, message: str) -> None:
    rec["status"] = "failed"
    rec["ended_at"] = time.time()
    rec["error"] = {"code": code, "message": message, "details": "\n".join(list(rec["log"])[-20:])}
    # Scratch is only worth keeping if there is audio in it. A failure that
    # captured nothing leaves nothing behind; one that captured a meeting and
    # then could not transcode it keeps the WAV, which orphans() will offer back.
    worth_keeping = _captured_bytes(rec) > EMPTY_WAV
    if worth_keeping:
        _checkpoint(rec)
    else:
        shutil.rmtree(rec["work"], ignore_errors=True)


def ask_to_stop(rec: dict, keep: bool, grace: float = 10.0) -> None:
    """Signal both captures to finish, and mean it if they do not.

    The escalation is the part worth sharing. A start that refuses itself used to
    signal by hand without it, and a capture blocked on a permission prompt ignored
    the signal and sat in "stopping" — which then answered the next press of record
    with "a recording is already running".
    """
    rec["keep"] = keep
    rec["status"] = "stopping"
    _signal(signal.SIGINT)  # ffmpeg finishes the file and exits; a kill would not
    asyncio.create_task(_insist(rec, grace))


# --- checking it works, before it matters --------------------------------------


CHECK_SECONDS = 6.0
# Loud enough to be unmistakable, short enough not to be annoying, and a tone rather
# than speech so nothing in it can be mistaken for somebody talking.
CHECK_TONE = "sine=frequency=440:duration=3"


def check_verdict(asked: list[str], loudest: dict[str, float], tone_played: bool = True) -> dict:
    """What each side heard while a sound of ours was playing, and what it means.

    The whole reason for playing that sound: nothing can tell a refused tap from a
    machine that happens to be quiet, because both deliver nothing. So the machine
    is made not quiet. If the tap cannot hear audio this app is playing through the
    output the tap is attached to, the silence is not the room's.
    """
    out = {}
    for side in asked:
        level = loudest.get(side)
        if level is None and side == "voice":
            out[side] = {"heard": False, "level": None, "why": "nothing"}
        elif level is None:
            # Nothing arrived at all. If our own tone played, the output device was
            # running and the tap was handed none of it, which is a refusal — a
            # process holding no audio grant gets no callbacks rather than silent
            # ones, so this is the second face of the same thing. If the tone never
            # played there is nothing to conclude but that.
            out[side] = {"heard": False, "level": None,
                         "why": "refused" if tone_played else "output"}
        elif side == "computer" and level <= DEAD_TAP:
            # Frames, and every one of them digital zero, while we were playing into
            # them. There is only one thing that does that.
            out[side] = {"heard": False, "level": level, "why": "refused"}
        elif level <= SILENT_LUFS:
            out[side] = {"heard": False, "level": level, "why": "quiet"}
        else:
            out[side] = {"heard": True, "level": level, "why": None}
    return out


def remember_check(sides: dict) -> None:
    """Whether the offer on the first screen has been answered.

    Only a check where every side asked for came back counts. A working microphone
    beside a refused tap is precisely the state somebody most needs offering again,
    so it puts the offer back rather than leaving it half-answered.
    """
    passed = bool(sides) and all(side["heard"] for side in sides.values())
    save_settings({"capture_checked": time.time() if passed else 0})


async def _play_test_tone(rec: dict) -> bool:
    """A sound of our own, through whatever the machine is playing out of.

    Whether it actually played is half the verdict: a tap that heard nothing while
    this was playing was refused, and a tap that heard nothing because nothing
    played has told us only that nothing played.
    """
    tone = rec["work"] / "tone.wav"
    make = [binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", CHECK_TONE, str(tone)]
    try:
        code, _ = await capture(make, timeout=30)
        if code != 0 or not tone.is_file():
            rec["log"].append("# the test tone could not be made")
            return False
        played, out = await capture(["/usr/bin/afplay", str(tone)], timeout=30)
        if played != 0:
            rec["log"].append(f"# the test tone would not play: {out.strip()[:120]}")
        return played == 0
    except (Failed, OSError) as exc:
        rec["log"].append(f"# the test tone could not be played: {exc}")
        return False


async def check(voice: str, computer: str) -> dict:
    """Record for a few seconds, playing a sound of our own, and report what arrived.

    Everything a real recording does — the same captures, the same permissions, the
    same prompts — except that it is thrown away and it happens when nothing is at
    stake. Which is the point: a permission that was never granted should cost six
    seconds on a quiet afternoon, not the first ten minutes of a meeting.
    """
    await start(voice, computer)
    rec = RECORDING
    if rec is None:
        raise Failed("not_recording", "The check could not start a recording.")
    rec["checking"] = True
    tone = asyncio.create_task(_play_test_tone(rec))
    loudest: dict[str, float] = {}
    deadline = time.monotonic() + CHECK_SECONDS
    while time.monotonic() < deadline and rec["status"] == "recording":
        for side, level in (rec.get("live") or {}).items():
            loudest[side] = max(loudest.get(side, -1000.0), level)
        await asyncio.sleep(0.1)
    asked = _asked_for(rec)
    try:
        played = await asyncio.wait_for(tone, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        played = False
    log = list(rec["log"])
    try:
        await stop(keep=False)
    except Failed:
        pass  # already over, which is fine: nothing was going to be kept
    sides = check_verdict(asked, loudest, played)
    remember_check(sides)
    return {"sides": sides, "log": log[-12:]}


async def stop(keep: bool = True) -> dict:
    """Ask ffmpeg to finish. The rest happens in the task that owns the recording."""
    rec = RECORDING
    if rec is None or rec["status"] != "recording":
        raise Failed("not_recording", "Nothing is being recorded.")
    ask_to_stop(rec, keep)
    return public()


async def _insist(rec: dict, grace: float = 10.0) -> None:
    """If SIGINT was ignored, stop meaning it. The WAV survives either way."""
    await asyncio.sleep(grace)
    if PROC is not None and PROC.returncode is None and rec["status"] == "stopping":
        rec["log"].append("# ffmpeg did not stop when asked, so it was killed")
        _signal(signal.SIGKILL)


def _signal(sig: int) -> None:
    for proc in list(PROCS) or [PROC]:
        _signal_proc(proc, sig)
    _signal_helper(sig)


def _signal_helper(sig: int) -> None:
    _signal_proc(HELPER, sig)


def _signal_proc(proc: asyncio.subprocess.Process | None, sig: int) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def dismiss() -> None:
    """Clear a finished recording off the screen. Never touches a live one."""
    global RECORDING
    if RECORDING is not None and RECORDING["status"] not in LIVE:
        RECORDING = None


def glance() -> str:
    """The whole state of the app in one short line, for the menu bar.

    Plain text rather than JSON, and that is the point: the thing reading it is a
    Rust process that otherwise needs no HTTP client and no JSON parser to show a
    clock in the menu bar. One line it can split on a space.

        idle
        recording 42
        saving 128
        working 63
        ready

    Seconds for a recording, whole percent for a transcription.
    """
    rec = RECORDING
    if rec is not None and rec["status"] in LIVE:
        return f"{rec['status']} {int(recorded_seconds(rec))}"
    import jobs  # here rather than at the top: jobs imports this module back
    job = jobs.JOB
    if job is not None and job["status"] in ("queued", "running", "cancelling"):
        return f"working {int(job.get('percent') or 0)}"
    return "idle"


async def toggle() -> dict:
    """Start recording what was chosen last time, or stop what is running.

    One call with nothing to say, because the menu bar has nowhere to ask. The
    devices come from the same place the interface fills its dropdowns from, so
    the tray records exactly what the window would have recorded.
    """
    if RECORDING is not None and RECORDING["status"] in ("recording", "paused"):
        return await stop(keep=True)
    chosen = await devices()
    voice = chosen["voice"]
    if not voice:
        # The same guess the window makes when nothing has been chosen yet: the
        # machine's own default input, never a loopback. Without it the menu bar
        # would quietly record one side of a conversation, which is the failure
        # this app exists to stop rather than commit.
        voice = next((d["id"] for d in chosen["devices"]
                      if d.get("default") and not d.get("loopback")), "")
    return await start(voice, chosen["computer"])


def recorded_seconds(rec: dict) -> float:
    """How much recording there is, which is not how long ago it started.

    Time spent paused was deliberately not recorded, so counting it would show a
    clock that disagrees with the file it is describing.
    """
    end = rec["ended_at"] or time.time()
    away = rec.get("paused_total", 0.0)
    if rec.get("paused_at"):
        away += end - rec["paused_at"]
    return max(0.0, end - rec["started_at"] - away)


async def pause(resume: bool | None = None) -> dict:
    """Stop counting, or start again. Told to the helper with a signal.

    A pause is not an interruption and is not treated like one. An interruption is
    kept as the silence it was, because the meeting carried on in the room and
    every timestamp after it has to survive. A pause is somebody saying this time
    does not belong to the recording, so it is closed up — nobody wants twenty
    minutes of silence in the middle because they stepped out of the room.
    """
    rec = RECORDING
    if rec is None or rec["status"] not in ("recording", "paused"):
        raise Failed("not_recording", "There is no recording to pause.")
    if HELPER is None or HELPER.returncode is not None:
        raise Failed("recording_failed",
                     "This recording cannot be paused, because it is not being captured by the "
                     "part of the app that knows how. Stop it and start again to get a pause.")
    wanted = (rec["status"] == "recording") if resume is None else (not resume)
    if wanted:
        _signal_helper(signal.SIGUSR1)
        rec["paused_at"] = time.time()
        rec["status"] = "paused"
    else:
        _signal_helper(signal.SIGUSR2)
        rec["paused_total"] = rec.get("paused_total", 0.0) + (time.time() - rec["paused_at"])
        rec["paused_at"] = None
        rec["status"] = "recording"
    return public()


def meters() -> dict:
    """Just the needles. Small on purpose: this is asked for many times a second.

    Separate from `public` because that carries the log, the levels, the sizes and
    everything else the page needs once a second — asking for all of it fifteen
    times a second to move a bar would redraw the whole screen to animate one of
    them.
    """
    rec = RECORDING
    if rec is None or rec["status"] != "recording":
        return {"recording": False, "peak": {}}
    return {"recording": True, "peak": rec.get("peak") or {}}


def public() -> dict | None:
    """What /api/state shows. Cheap enough to answer once a second."""
    rec = RECORDING
    if rec is None:
        return None
    try:
        recorded = rec["wav"].stat().st_size
    except OSError:
        # While recording there is no master yet, so what has arrived is whatever
        # the captures have written between them.
        recorded = _captured_bytes(rec)
    return {
        "id": rec["id"], "status": rec["status"], "error": rec["error"],
        "started_at": rec["started_at"], "ended_at": rec["ended_at"],
        "path": rec["path"], "job_id": rec["job_id"],
        "labels": rec["labels"], "stereo": len(rec.get("sources") or rec["devices"]) == 2,
        "seconds": round(recorded_seconds(rec), 1),
        "bytes": recorded, "max_seconds": rec["max_seconds"],
        # Named by side rather than by channel number, so the interface can say
        # which of the two it was without knowing how they were arranged.
        "levels": rec.get("levels") or {}, "quiet": rec.get("quiet") or [],
        # What each source is hearing right now, in LUFS, while it records.
        "live": rec.get("live") or {},
        # Sides that were asked for and are producing nothing at all. Given a few
        # seconds' grace first, so that the ordinary lag of a capture starting is
        # not announced as a fault to somebody who has only just pressed record.
        # Not while the check is running: it plays its own sound, waits, and gives
        # one verdict at the end. A warning racing that verdict is two answers to
        # one question, and the louder one was wrong.
        "not_arriving": [side for side in _asked_for(rec)
                         if not _side_arriving(rec, side)]
        if rec["status"] == "recording" and not rec.get("checking")
        and time.time() - rec["started_at"] > SETTLING else [],
        # Sides that were arriving and have stopped, which the warning above cannot
        # see: it asks whether a side ever started, and a level that stopped being
        # updated still reads as a healthy one.
        "stalled": stalled_sides(rec) if rec["status"] == "recording"
        and not rec.get("checking") else [],
        "log": list(rec["log"]),
    }


# --- recordings the process did not live long enough to save ------------------


def _checkpoint(rec: dict) -> None:
    """Leave enough on disk to save the WAV later if this process dies now."""
    record = {k: rec[k] for k in ("id", "status", "devices", "labels", "folder",
                                 "basename", "started_at", "transcribe")}
    try:
        (rec["work"] / "recording.json").write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass  # a recording that cannot checkpoint should still record


def orphans() -> list[dict]:
    """Captured audio that was never turned into a file, newest first.

    A crash between starting and stopping leaves a perfectly good WAV in scratch.
    Because it is a WAV and not an .m4a it is still playable, so it is offered
    back rather than swept away.
    """
    live = (RECORDING or {}).get("id")
    out = []
    for meta in WORK_DIR.glob(f"{RECORDING_PREFIX}*/recording.json"):
        try:
            record = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recorded = _captured_bytes({"voice_wav": meta.parent / "voice.wav",
                                    "computer_wav": meta.parent / "computer.wav",
                                    "sys_pcm": meta.parent / "computer.pcm"})
        if record.get("id") == live or recorded <= EMPTY_WAV:
            continue
        out.append({
            "id": record["id"],
            "started_at": record.get("started_at"),
            "bytes": recorded,
            "stereo": len(record.get("devices") or []) == 2,
            # An estimate, and only that: 16-bit at 48 kHz per side. The microphone
            # is recorded at whatever rate it offers rather than a rate we chose,
            # so its own size no longer says exactly how long it ran.
            "seconds": round(recorded / (2 * 48000 * max(1, len(record.get("devices") or [1]))), 1),
        })
    return sorted(out, key=lambda r: -(r["started_at"] or 0))


def _orphan(rec_id: str) -> dict | None:
    work = WORK_DIR / f"{RECORDING_PREFIX}{Path(rec_id).name}"
    try:
        record = json.loads((work / "recording.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (RECORDING or {}).get("id") == record.get("id"):
        return None
    return {**record, "work": work, "wav": work / "master.wav",
            "voice_wav": work / "voice.wav", "computer_wav": work / "computer.wav",
            "sys_pcm": work / "computer.pcm", "voice_pcm": work / "voice.pcm",
            "keep": True, "ended_at": None, "path": None, "job_id": None,
            "error": None, "max_seconds": 0, "log": deque(maxlen=MAX_LOG)}


async def keep_orphan(rec_id: str) -> dict:
    """Finish the job the crash interrupted: save the WAV and queue it."""
    global RECORDING
    rec = _orphan(rec_id)
    if rec is None:
        raise Failed("invalid_input_path", "That recording is no longer on disk.")
    if RECORDING is not None and RECORDING["status"] in LIVE:
        raise Failed("already_recording", "Finish the recording that is running first.")
    RECORDING = rec
    # The same ending the interrupted recording never reached: combine the two
    # captures, then save. A crash leaves them side by side and nothing else.
    await _finish(rec)
    if rec["status"] == "failed":
        raise Failed(rec["error"]["code"], rec["error"]["message"])
    return public()


def discard_orphan(rec_id: str) -> dict:
    shutil.rmtree(WORK_DIR / f"{RECORDING_PREFIX}{Path(rec_id).name}", ignore_errors=True)
    return {"ok": True}
