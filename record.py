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

LIVE = ("recording", "stopping", "saving")
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
SILENT_LUFS = -70.0

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


async def devices() -> dict:
    """Audio inputs this machine offers, and what is missing if anything is.

    Never called from /api/state: listing devices spawns ffmpeg, and on macOS it
    opens each device briefly. Once per visit to the Record view is enough.
    """
    # `-list_devices true` has no input to process, so ffmpeg prints the list and
    # then exits non-zero. The code says nothing about whether listing worked.
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
ONE_STREAM = ("aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono"
              ",aresample=async=1000:first_pts=0")


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
            + ["-t", str(rec["max_seconds"]),
               "-af", "ebur128=metadata=1,ametadata=print:key=lavfi.r128.M",
               "-f", "null", "-"]
            + ["-t", str(rec["max_seconds"]), "-c:a", "pcm_s16le", str(out)])


def capture_commands(rec: dict) -> list[list[str]]:
    """One ffmpeg per real device. The driverless source is the helper's job.

    One process each rather than one process with two inputs, which is the whole
    change: a single ffmpeg had to reconcile two clocks as the audio arrived, and
    filled the difference with silence.
    """
    out = []
    for device, path in ((rec["voice"], rec["voice_wav"]),
                         (rec["computer"], rec["computer_wav"])):
        if device and device != SYSTEM_AUDIO:
            out.append(capture_command(rec, device, path))
    return out


def _captured_bytes(rec: dict) -> int:
    """How much audio has arrived, across whichever sources are running."""
    total = 0
    for key in ("voice_wav", "computer_wav", "sys_pcm"):
        path = rec.get(key)
        try:
            total += path.stat().st_size
        except (OSError, AttributeError):
            pass
    return total


def captured_sources(rec: dict) -> list[str]:
    """Which sides actually recorded something, in channel order: voice, then computer."""
    out = []
    for keys, label in ((("voice_wav",), "voice"), (("computer_wav", "sys_pcm"), "computer")):
        for key in keys:
            path = rec.get(key)
            try:
                if path is not None and path.stat().st_size > EMPTY_WAV:
                    out.append(label)
                    break
            except OSError:
                continue
    return out


def mix_command(rec: dict, sources: list[str]) -> list[str]:
    """Combine the finished captures into the stereo master that gets kept.

    Offline, from complete files, which is the whole point: there are no clocks
    left to chase, so aresample corrects the drift between the two once instead of
    guessing at it thousands of times while recording.
    """
    cmd = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]
    if "voice" in sources:
        cmd += ["-i", str(rec["voice_wav"])]
    if "computer" in sources:
        if rec.get("computer_wav") is not None and rec["computer_wav"].is_file() \
                and rec["computer_wav"].stat().st_size > EMPTY_WAV:
            cmd += ["-i", str(rec["computer_wav"])]
        else:
            # Raw samples carry no header, so the format the helper promised is stated.
            cmd += ["-f", "s16le", "-ar", "48000", "-ac", "1", "-i", str(rec["sys_pcm"])]
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
                     f"Recordings cannot be written to {folder}: {exc.strerror or exc}")
    if not os.access(folder, os.W_OK):
        raise Failed("insufficient_permissions", f"Recordings folder is not writable: {folder}")

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
        "log": deque(maxlen=MAX_LOG),
    }
    RECORDING = rec
    _checkpoint(rec)
    TASK = asyncio.create_task(_run(rec))
    await _until_audio_arrives(rec)
    if rec["status"] == "failed":
        raise Failed(rec["error"]["code"], rec["error"]["message"])
    # Remember the choice by name, so the next recording is one decision lighter and
    # still points at the same device after something is plugged in or unplugged.
    try:
        _, listing = await capture(_list_command(), timeout=30)
        known = _parse_avfoundation(listing) if sys.platform == "darwin" \
            else _parse_pulse(listing)
    except (Failed, OSError):
        known = []
    save_settings({"record_voice_device": name_for(rec["voice"], known),
                   "record_computer_device": name_for(rec["computer"], known)})
    return public()


async def _until_audio_arrives(rec: dict, timeout: float = 3.0) -> None:
    """Wait for the file to start growing, so a refusal is reported by the click.

    Without this, a denied microphone permission looks like a recording that
    started fine and turns out, minutes later, to be nothing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rec["status"] != "recording":
            return
        if _captured_bytes(rec) > EMPTY_WAV:
            return
        await asyncio.sleep(0.1)


async def _start_helper(rec: dict) -> bool:
    """Begin capturing the computer's audio into the FIFO ffmpeg is about to read.

    It writes to a file of its own, so it neither waits for ffmpeg nor races it.
    """
    global HELPER
    cmd = [str(rec["helper"]), str(rec["sys_pcm"])]
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
            if line:
                rec["log"].append(line)
        await proc.wait()
    finally:
        if HELPER is proc:
            HELPER = None
        rec["helper_code"] = proc.returncode


async def _run(rec: dict) -> None:
    """One recording, start to finish: capture, then save what was captured."""
    global PROC
    if rec.get("helper") is not None and not await _start_helper(rec):
        return
    commands = capture_commands(rec)
    if not commands:
        # The computer's audio and nothing else: the helper is the whole capture,
        # so there is no ffmpeg to wait on.
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
    while rec["status"] == "recording":
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
            continue
        rec["log"].append(line)
    await proc.wait()
    rec.setdefault("live", {}).pop(label, None)


async def _await_helper(rec: dict, poll: float = 0.2) -> None:
    """Wait out a recording that only the helper is making."""
    # Measured from when the recording began, so a source lost halfway does not
    # quietly grant the rest another full allowance.
    deadline = rec["started_at"] + rec["max_seconds"]
    while time.time() < deadline:
        if HELPER is None or HELPER.returncode is not None:
            return
        if rec["status"] not in ("recording", "stopping"):
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
    if not await _mix(rec, sources):
        return
    # Reached even when a capture died of its own accord: whatever arrived before
    # it stopped is still a recording, and still worth keeping.
    await _save(rec)


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
    rec["quiet"] = quiet_sides(rec["levels"])
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


async def stop(keep: bool = True) -> dict:
    """Ask ffmpeg to finish. The rest happens in the task that owns the recording."""
    rec = RECORDING
    if rec is None or rec["status"] != "recording":
        raise Failed("not_recording", "Nothing is being recorded.")
    rec["keep"] = keep
    rec["status"] = "stopping"
    _signal(signal.SIGINT)  # ffmpeg finishes the file and exits; a kill would not
    asyncio.create_task(_insist(rec))
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
        "seconds": round((rec["ended_at"] or time.time()) - rec["started_at"], 1),
        "bytes": recorded, "max_seconds": rec["max_seconds"],
        # Named by side rather than by channel number, so the interface can say
        # which of the two it was without knowing how they were arranged.
        "levels": rec.get("levels") or {}, "quiet": rec.get("quiet") or [],
        # What each source is hearing right now, in LUFS, while it records.
        "live": rec.get("live") or {},
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
            "sys_pcm": work / "computer.pcm",
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
