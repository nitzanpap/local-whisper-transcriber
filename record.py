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
# The system-audio helper, when one is feeding ffmpeg. Stopped with ffmpeg, and
# separately, because it is a sibling of the recording rather than a child of it.
HELPER: asyncio.subprocess.Process | None = None

LIVE = ("recording", "stopping", "saving")
MAX_LOG = 120

# A WAV shorter than this is a header and nothing else.
EMPTY_WAV = 2048

# The helper's own exit code for a permission macOS would not give. Kept in step
# with DENIED in mac/syscapture.swift.
HELPER_DENIED = 3

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
            [swiftc, "-O", "-parse-as-library", "-o", str(staged), str(HELPER_SOURCE)],
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


async def _granted(helper: Path, prompt: bool) -> bool:
    """Whether Screen Recording is allowed. `prompt` is the only thing that asks."""
    try:
        code, out = await capture([str(helper), "--request" if prompt else "--probe"],
                                  timeout=120 if prompt else 20)
    except (Failed, OSError):
        return False
    if code != 0:
        return False
    for line in reversed(out.splitlines()):
        try:
            return bool(json.loads(line).get("granted"))
        except ValueError:
            continue
    return False


async def system_audio(prompt: bool = False) -> dict | None:
    """The driverless computer-audio source, or None where there cannot be one."""
    helper = await helper_path()
    if helper is None:
        return None
    return {"helper": helper, "granted": await _granted(helper, prompt)}


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
    conf = recording_config()
    advice = []
    if not found:
        advice.append("noDevices")
    elif sysaudio is not None and not sysaudio["granted"]:
        # A permission that has not been given yet, which is one click rather than
        # a driver to install — so it is worth saying instead of the older advice.
        advice.append("needScreenRecording")
    elif not any(d["loopback"] for d in found):
        advice.append("needLoopback")
    return {
        "devices": found,
        "voice": conf["voice"],
        "computer": conf["computer"],
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
# aresample keeps two independently clocked devices from drifting apart over an
# hour: each capture session has its own clock, and without this the two channels
# would slowly separate — which is exactly what an Aggregate Device's drift
# correction exists to prevent in hardware.
ONE_STREAM = ("aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono"
              ",aresample=async=1000:first_pts=0")


def _input_args(device: str, rec: dict) -> list[str]:
    if device == SYSTEM_AUDIO:
        # A FIFO carrying raw samples has no header to describe itself, so the
        # format the helper promised is stated here instead. Wallclock timestamps
        # for the same reason as a real device: it is a live source, and aresample
        # needs something to correct against.
        return ["-f", "s16le", "-ar", "48000", "-ac", "1",
                "-use_wallclock_as_timestamps", "1", "-i", str(rec["fifo"])]
    # No wallclock timestamps on a real device, which was a mistake worth naming.
    # A capture device hands its audio over in bursts, and stamping each burst with
    # the time it arrived describes a stream full of gaps that were never in the
    # sound. aresample below then honours that description and fills the gaps with
    # silence, which is audible as a stutter and, worse, quietly destroys speech:
    # a voice chopped into 150 ms pieces reads to VAD as no voice at all. The
    # device's own sample clock is continuous, so letting it supply the timestamps
    # is both simpler and correct — aresample still corrects the drift between two
    # devices, which is the only thing it was ever needed for.
    if sys.platform == "darwin":
        # ":N" is avfoundation for "no video, audio device N".
        return ["-f", "avfoundation", "-i", f":{device}"]
    return ["-f", "pulse", "-i", device]


def capture_command(rec: dict) -> list[str]:
    cmd = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    for device in rec["devices"]:
        cmd += _input_args(device, rec)
    if len(rec["devices"]) == 2:
        graph = (f"[0:a]{ONE_STREAM}[voice];[1:a]{ONE_STREAM}[computer];"
                 "[voice][computer]join=inputs=2:channel_layout=stereo[out]")
    else:
        graph = f"[0:a]{ONE_STREAM}[out]"
    # -t so a recording forgotten about stops itself rather than filling the disk.
    return cmd + ["-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le",
                  "-t", str(rec["max_seconds"]), str(rec["wav"])]


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
        # Asked for by the click that starts the recording, not silently at startup:
        # a permission prompt out of nowhere is worse than one with a reason.
        sysaudio = await system_audio(prompt=True)
        if sysaudio is None:
            raise Failed("dependency_not_found",
                         "The system-audio helper could not be built. Xcode's command line "
                         "tools provide the compiler it needs (xcode-select --install).")
        if not sysaudio["granted"]:
            raise Failed("insufficient_permissions",
                         "macOS has not allowed this app to capture the computer's audio. Open "
                         "System Settings → Privacy & Security → Screen Recording, allow it "
                         "there, then start the app again and record.")
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
        "wav": work / "master.wav",
        "helper": helper,
        "fifo": work / "system.pcm",
        "log": deque(maxlen=MAX_LOG),
    }
    if helper is not None:
        try:
            os.mkfifo(rec["fifo"])
        except OSError as exc:
            raise Failed("recording_failed",
                         f"The pipe for the computer's audio could not be made: {exc}")
    RECORDING = rec
    _checkpoint(rec)
    TASK = asyncio.create_task(_run(rec))
    await _until_audio_arrives(rec)
    if rec["status"] == "failed":
        raise Failed(rec["error"]["code"], rec["error"]["message"])
    # Remember the choice, so the next recording is one decision lighter.
    save_settings({"record_voice_device": rec["voice"],
                   "record_computer_device": rec["computer"]})
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
        try:
            if rec["wav"].stat().st_size > EMPTY_WAV:
                return
        except OSError:
            pass
        await asyncio.sleep(0.1)


async def _start_helper(rec: dict) -> bool:
    """Begin capturing the computer's audio into the FIFO ffmpeg is about to read.

    Started before ffmpeg deliberately: opening a FIFO for writing blocks until
    something opens it for reading, so this waits for ffmpeg rather than racing it.
    """
    global HELPER
    cmd = [str(rec["helper"]), str(rec["fifo"])]
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
    cmd = capture_command(rec)
    rec["log"].append("$ " + shlex.join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return _failed(rec, "ffmpeg_failed", f"ffmpeg would not start: {exc}")
    PROC = proc
    try:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                rec["log"].append(line)
        await proc.wait()
    finally:
        PROC = None
        # ffmpeg is done reading, so the helper has nobody to write to. It would
        # notice on its own at the next buffer, but not being asked to linger is
        # cheaper than waiting for that.
        _signal_helper(signal.SIGINT)

    if not rec["keep"]:
        rec["status"] = "discarded"
        rec["ended_at"] = time.time()
        shutil.rmtree(rec["work"], ignore_errors=True)
        return
    try:
        recorded = rec["wav"].stat().st_size
    except OSError:
        recorded = 0
    if recorded <= EMPTY_WAV:
        return _failed(rec, *_why_nothing_arrived(rec))
    # Reached even when ffmpeg died of its own accord: whatever was captured
    # before it stopped is still a recording, and still worth keeping.
    await _save(rec)


def _why_nothing_arrived(rec: dict) -> tuple[str, str]:
    text = " ".join(rec["log"]).lower()
    # The helper reports a refused permission as its exit code, so this is known
    # rather than guessed from log text. Checked first: when the computer's audio
    # was never allowed, nothing else that went wrong afterwards is the cause.
    if rec.get("helper_code") == HELPER_DENIED:
        return ("insufficient_permissions",
                "macOS did not let this app capture the computer's audio. Open System "
                "Settings → Privacy & Security → Screen Recording, allow it there, then "
                "start the app again and record.")
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
    stereo = len(rec["devices"]) == 2
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
        shutil.move(str(staged), str(final))
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
    if len(rec["devices"]) == 2:
        # Left is the voice, right is the machine — the order capture_command used.
        tracks = [{"channel": 0, "label": rec["labels"][0]},
                  {"channel": 1, "label": rec["labels"][1]}]
    else:
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
    try:
        worth_keeping = rec["wav"].stat().st_size > EMPTY_WAV
    except OSError:
        worth_keeping = False
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
    _signal_proc(PROC, sig)
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
        recorded = 0
    return {
        "id": rec["id"], "status": rec["status"], "error": rec["error"],
        "started_at": rec["started_at"], "ended_at": rec["ended_at"],
        "path": rec["path"], "job_id": rec["job_id"],
        "labels": rec["labels"], "stereo": len(rec["devices"]) == 2,
        "seconds": round((rec["ended_at"] or time.time()) - rec["started_at"], 1),
        "bytes": recorded, "max_seconds": rec["max_seconds"],
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
            recorded = (meta.parent / "master.wav").stat().st_size
        except (OSError, ValueError):
            continue
        if record.get("id") == live or recorded <= EMPTY_WAV:
            continue
        out.append({
            "id": record["id"],
            "started_at": record.get("started_at"),
            "bytes": recorded,
            "stereo": len(record.get("devices") or []) == 2,
            # 48 kHz, 16-bit, one or two channels: the WAV's own size is the clock.
            "seconds": round(recorded / (2 * 48000 * (len(record.get("devices") or [1]))), 1),
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
    await _save(rec)
    if rec["status"] == "failed":
        raise Failed(rec["error"]["code"], rec["error"]["message"])
    return public()


def discard_orphan(rec_id: str) -> dict:
    shutil.rmtree(WORK_DIR / f"{RECORDING_PREFIX}{Path(rec_id).name}", ignore_errors=True)
    return {"ok": True}
