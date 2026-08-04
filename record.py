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

What is left in this file is the part that has to be here: the one recording
that exists at a time, the processes making it, and the state it moves through.
Everything that could be asked without a recording running was moved out, and
the modules below are that split, bottom up —

    syshelper   the Swift capture helper: where it lives, what it says
    devices     what this machine offers to record from
    levels      how loud a thing was, and whether audio is still arriving
    mixing      the ffmpeg commands, as pure functions of a recording
    saving      everything after the audio stops: repair, mix, keep, queue
    selfcheck   the test tone, and reading what came back from it

Nothing in that list imports this file. That is deliberate and worth keeping:
the moment one of them needs the live recording, it belongs back in here.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import sys
import time
import uuid
from collections import deque
from pathlib import Path

import saving
import selfcheck
import syshelper
from config import (Failed, MAX_LOG, RECORDING_PREFIX, WORK_DIR, recording_config,
                    save_settings)
from devices import devices, known_inputs, name_for
from levels import (DIGITAL_SILENCE, EBUR128_M, EMPTY_WAV, PEAK, _captured_bytes,
                    _heard, stalled_sides)
from mixing import capture_commands, helper_takes, helper_takes_the_microphone
from saving import _checkpoint, _failed, _finish, _stamp
from syshelper import HELPER_LEVEL, HELPER_OUTPUT, SYSTEM_AUDIO

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


# A recording is stopped once the disk has less than this left, which is roughly
# ten minutes of two sources. Stopping early keeps a meeting that is mostly there;
# filling the disk loses the end of it and can take the machine down with it.
LOW_DISK = 400_000_000


def disk_is_low(free_bytes: int) -> bool:
    return free_bytes < LOW_DISK


# --- recording ---------------------------------------------------------------


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
    if sys.platform == "darwin" and SYSTEM_AUDIO not in chosen:
        sysaudio = await syshelper.system_audio()
        helper = sysaudio["helper"] if sysaudio is not None else None
    if SYSTEM_AUDIO in chosen:
        # macOS is asked by the click that starts the recording, not silently at
        # startup: a permission prompt out of nowhere is worse than one with a
        # reason. There is nothing to check beforehand — a process tap is created
        # whether or not it is allowed, and an unallowed one just returns silence.
        sysaudio = await syshelper.system_audio()
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
    known, _ = await known_inputs()
    # Only what was actually chosen. A device that could not be seen at this moment
    # must not erase the one that was remembered — clearing the microphone grant
    # emptied the device listing, which wrote an empty choice over a good one, and
    # every recording afterwards was the computer's side alone with no word said.
    remember = {}
    for key, side in (("record_voice_device", "voice"),
                      ("record_computer_device", "computer")):
        name = name_for(rec[side], known)
        # Kept on the recording as well as in settings. An id on its own is what
        # the Teams-loopback fault looked like from outside: a stored string
        # nobody could read as "that is not the computer's audio at all".
        rec.setdefault("device_names", {})[side] = name
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
        # itself. See selfcheck.check_verdict. Here, the honest question is the smaller one.
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
    if helper_takes(rec, "voice"):
        # By UID. Positions move when anything is plugged in; a stored index has
        # already meant two different microphones on this machine.
        cmd += ["--mic", str(rec["voice_pcm"]), "--mic-device", rec["voice"]]
    if helper_takes(rec, "computer"):
        # Into the same file the tap would have used: to everything downstream this
        # is the computer's side, however it was captured.
        cmd += ["--other", str(rec["sys_pcm"]), "--other-device", rec["computer"]]
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
            said = HELPER_OUTPUT.search(line)
            if said:
                rec["output_device"] = said.group(1)
            found = HELPER_LEVEL.search(line)
            if found:
                # Kept out of the log, like ffmpeg's: ten lines a second would
                # bury everything worth reading.
                side, level = found.group(1), float(found.group(2))
                # The one place a helper-captured side can be seen to still be
                # arriving. Without this the live "gone quiet" warning was wired
                # only to the ffmpeg reader — and on macOS the helper captures
                # both sides, so nothing on this machine could ever trip it. A tap
                # that died when the output device changed went unmentioned for
                # 75 seconds of a 132-second recording; see docs/TRAPS.md.
                _heard(rec, side, already_padded=True)
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


# How long a capture is allowed to take to produce its first audio before the
# recording screen says out loud that it is producing none. Long enough to cover an
# ordinary start, short enough that nobody talks for a minute into nothing.
SETTLING = 4.0


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
    tone = asyncio.create_task(selfcheck.play_test_tone(rec))
    loudest: dict[str, float] = {}
    deadline = time.monotonic() + selfcheck.CHECK_SECONDS
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
    sides = selfcheck.check_verdict(asked, loudest, played)
    selfcheck.remember_check(sides)
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
        "snr": rec.get("snr") or {}, "noisy": rec.get("noisy") or [],
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


def orphans() -> list[dict]:
    """Captured audio that was never turned into a file, newest first.

    The one being made right now is not one of them, which is the only thing the
    reader in `saving` cannot work out for itself.
    """
    return saving.orphans((RECORDING or {}).get("id"))


async def keep_orphan(rec_id: str) -> dict:
    """Finish the job the crash interrupted: save the WAV and queue it."""
    global RECORDING
    rec = saving.orphan(rec_id, (RECORDING or {}).get("id"))
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
    """Throw away audio that was never saved.

    Here rather than imported from `saving`, so that the app asks the recorder
    about recordings and never has to know which of six files answers.
    """
    return saving.discard_orphan(rec_id)
