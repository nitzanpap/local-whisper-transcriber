"""Everything after the audio stops arriving: repair it, mix it, keep it.

Split out of the recorder because none of it touches the live state. Each function
here takes the recording dict and works on the files named in it, which is what
makes the same path serve two callers that look nothing alike — a recording that
ended because somebody pressed stop, and one rescued from the scratch directory
after the app died mid-meeting. The second is not a special case, it is this same
ending arriving late.

The order matters and is not obvious. Padding comes before mixing, because a
capture that stalled has to get its missing silence back *before* two tracks are
laid against each other — otherwise everything said after the stall sits earlier
in one channel than in the other. Saving comes last, and only ever from a complete
master.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import time
import uuid
from collections import deque
from pathlib import Path

import diagnostics
import jobs
import retention
import watch
from config import (DEFAULT_EXTRA, Failed, MAX_LOG, RECORDING_PREFIX,
                    TRANSCRIPT_SUFFIX, WORK_DIR, recording_config, settings)
from levels import (EMPTY_WAV, LOW_SNR, _captured_bytes, captured_sources,
                    channel_levels, channel_snr, noisy_sides, padded_sides,
                    silent_sides)
from mixing import mix_command, pad_command
from syshelper import HELPER_DENIED, SYSTEM_AUDIO
from tools import binary, capture
from transcribe import duration_seconds


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


async def _keep_what_arrived(rec: dict) -> None:
    """Save the unpadded copy of any side that padded too much to be usable.

    The helper writes every side twice: once padded to the clock, which is the
    recording, and once as only what the device handed over. The second is thrown
    away with the scratch directory nearly always, and rightly — it is the same
    audio and half the point of the padding is that timestamps survive a pause.

    It is kept when a side padded past `LOW_SNR`’s sibling threshold, because
    then the padding is not standing in for a pause, it is standing in for the
    meeting. Measured on the one that made this necessary: a two-hour client call
    where the computer side ran at 14-28% padding and came back as speech chopped
    into fragments, unintelligible and untranscribable, with nothing to go back to.
    This is that something to go back to. The timing in it is wrong — everything
    that never arrived is simply absent — but the words are there, which is the
    part that could not be recovered any other way.
    """
    for side in padded_sides(rec.get("padding") or {}):
        source = rec.get("voice_pcm" if side == "voice" else "sys_pcm")
        raw = Path(str(source) + ".raw") if source else None
        if raw is None or not raw.is_file() or raw.stat().st_size <= EMPTY_WAV:
            continue
        share = (rec["padding"][side] or {}).get("fraction", 0)
        kept = _unique(Path(rec["folder"]) /
                       f"{rec['basename']} ({side} as it arrived).m4a")
        cmd = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", str(raw),
               "-c:a", "aac", "-b:a", "96k", str(kept)]
        try:
            code, _ = await capture(cmd, timeout=1800)
        except (Failed, OSError) as exc:
            rec["log"].append(f"# the unpadded {side} copy could not be saved: {exc}")
            continue
        if code != 0 or not kept.is_file():
            rec["log"].append(f"# the unpadded {side} copy could not be saved")
            continue
        rec.setdefault("rescued", {})[side] = str(kept)
        rec["log"].append(
            f"# {share * 100:.0f}% of the {side} side was padding, so what actually arrived "
            f"was kept beside the recording as {kept.name} — the timing in it is wrong "
            f"and the words are not")


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
    # And how far the talking sat above the room, which is the measure that decides
    # whether the transcript will be any good — measured, the difference between a
    # full transcript and a single word. Said now, while there is still a next
    # meeting to move the microphone for; nothing done afterwards can add it back.
    await _keep_what_arrived(rec)
    rec["snr"] = await channel_snr(rec["wav"], sources)
    rec["noisy"] = [side for side in noisy_sides(rec["snr"]) if side not in rec["quiet"]]
    for side in rec["noisy"]:
        rec["log"].append(
            f"# the {side} side is only {rec['snr'][side]:.0f} dB above its own background, "
            f"and below about {LOW_SNR:.0f} dB the transcript starts losing words")
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
    # Now, rather than on the settings screen. Saving a recording is the act that
    # creates the surplus, so it is the moment to clear it — and it means the only
    # deletion this app does by itself happens once the thing being kept is
    # already safe, never while somebody is deciding what to set.
    #
    # Off the loop for the reason the move above is: the recordings folder can be
    # one macOS guards, and unlink blocks until the consent dialog is answered.
    keep = recording_config()["keep"]
    if keep:
        busy = {job["source"] for job in jobs.QUEUE}
        gone = await asyncio.to_thread(retention.prune, Path(rec["folder"]), keep, busy)
        if gone:
            rec["log"].append(f"# keeping the last {keep}, so these went: " + ", ".join(gone))
    # Written down before the scratch directory takes the log with it. Everything
    # this project has diagnosed by hand was knowable here and thrown away one
    # line later; see the note at the top of diagnostics.py. Off the loop because
    # it reads the whole recording back, and never allowed to fail the save.
    await asyncio.to_thread(diagnostics.remember, rec)
    await asyncio.to_thread(diagnostics.trim)
    # The WAV has served its purpose; the .m4a is out of scratch and safe.
    shutil.rmtree(rec["work"], ignore_errors=True)


async def enqueue(rec: dict, path: Path) -> str | None:
    """Queue the recording for transcription, one track per source."""
    conf = settings()
    model = conf.get("default_model_path", "")
    if not model or not Path(model).is_file():
        rec["log"].append("# no model chosen yet, so the recording was not queued")
        return None
    # From what actually captured, not from the file: this knows a side was asked
    # for and stayed silent, which `tracks_for` can only infer afterwards. The two
    # must agree on a recording with both sides, and the suite checks that they do.
    sources = rec.get("sources") or ["voice", "computer"][:len(rec["devices"])]
    if len(sources) == 2:
        tracks = jobs.two_tracks(rec["labels"])
    else:
        # One side only, so there is nobody to tell apart and no label to carry.
        tracks = list(jobs.ONE_TRACK)
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


def _checkpoint(rec: dict) -> None:
    """Leave enough on disk to save the WAV later if this process dies now."""
    record = {k: rec[k] for k in ("id", "status", "devices", "labels", "folder",
                                 "basename", "started_at", "transcribe")}
    try:
        (rec["work"] / "recording.json").write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass  # a recording that cannot checkpoint should still record


# --- and reading those checkpoints back, after a crash ------------------------
#
# The live recording is passed in by id rather than looked up. Nothing here can
# see the recorder's state and nothing here should: what "in progress" means is
# the recorder's business, and the one thing these two need to know about it is
# small enough to say out loud.


def orphans(live_id: str | None = None) -> list[dict]:
    """Captured audio that was never turned into a file, newest first.

    A crash between starting and stopping leaves a perfectly good WAV in scratch.
    Because it is a WAV and not an .m4a it is still playable, so it is offered
    back rather than swept away.
    """
    out = []
    for meta in WORK_DIR.glob(f"{RECORDING_PREFIX}*/recording.json"):
        try:
            record = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recorded = _captured_bytes({"voice_wav": meta.parent / "voice.wav",
                                    "computer_wav": meta.parent / "computer.wav",
                                    "sys_pcm": meta.parent / "computer.pcm"})
        if record.get("id") == live_id or recorded <= EMPTY_WAV:
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


def orphan(rec_id: str, live_id: str | None = None) -> dict | None:
    """One orphan, rebuilt into the shape the rest of this file expects."""
    work = WORK_DIR / f"{RECORDING_PREFIX}{Path(rec_id).name}"
    try:
        record = json.loads((work / "recording.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if live_id is not None and live_id == record.get("id"):
        return None
    return {**record, "work": work, "wav": work / "master.wav",
            "voice_wav": work / "voice.wav", "computer_wav": work / "computer.wav",
            "sys_pcm": work / "computer.pcm", "voice_pcm": work / "voice.pcm",
            "keep": True, "ended_at": None, "path": None, "job_id": None,
            "error": None, "max_seconds": 0, "log": deque(maxlen=MAX_LOG)}


def discard_orphan(rec_id: str) -> dict:
    shutil.rmtree(WORK_DIR / f"{RECORDING_PREFIX}{Path(rec_id).name}", ignore_errors=True)
    return {"ok": True}
