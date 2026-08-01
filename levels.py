"""How loud a thing was, and what that means.

This exists because nothing else can tell the difference. A recording made from a
device that was asleep, or muted, or simply the wrong one is exactly the same size
as a good one and reports exactly the same success. Hours went into a
transcription problem that turned out to be a microphone recording digital zero,
and every check here is descended from that afternoon.

Two meters, because they answer different questions. `ebur128` says how loud a
thing is the way a broadcaster means it, and everything that judges a finished
recording is built on it — but it cannot show a voice moving, because momentary
loudness is defined over a 400 ms window and 400 ms is about two syllables. Peak
over 50 ms windows is what the needle on screen moves on.

The other half is arrival rather than loudness: `_heard` and `stalled_sides` watch
whether audio is still coming at all, which is a different question from whether
anybody is talking.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from config import Failed
from syshelper import SYSTEM_AUDIO
from tools import binary, capture

# A WAV shorter than this is a header and nothing else.
EMPTY_WAV = 2048


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


# Where a voice stops being clearly above the room, measured rather than chosen.
#
# The same 90 seconds of real speech was transcribed at ten signal-to-noise ratios
# with the level held still. From 52 dB down to 20 dB nothing changed: 72-76
# seconds of speech found each time, 206-229 words, and the differences between
# neighbouring rungs as large as between distant ones, which is whisper's own
# run-to-run variation rather than anything to do with noise. Then it falls off a
# cliff — 15.7 dB found 61 s of speech instead of 75, and 11.1 dB found 15 s and
# produced a single word. 7.2 dB produced nothing at all.
#
# 18 is set between the flat part and the first rung that lost anything. It is a
# warning and not a refusal: what it names cannot be fixed after the fact, only
# before, which is why it is worth saying at all.
LOW_SNR = 18.0

# 100 ms of a 48 kHz stream. Long enough to hold a syllable, short enough that the
# gaps between words are their own windows rather than being averaged into speech.
SNR_WINDOW = 4800

RMS_LEVEL = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?\d+(?:\.\d+)?|-?inf)")


async def channel_snr(path: Path, sources: list[str]) -> dict[str, float | None]:
    """How far each side's talking sits above its own quiet, in dB.

    The one measurement that says whether a transcript is going to be any good,
    and the one thing no amount of processing afterwards can improve: turning a
    quiet channel up turns its hiss up with it. Measured across the same file at
    ten ratios, level alone changed nothing over a 35 dB range — this is what
    mattered instead.

    The 90th percentile of 100 ms windows is the talking and the 10th is the room
    between words. Percentiles rather than max and min because one door slam
    should not become the signal, and one impossibly quiet window should not
    become the floor.
    """
    out: dict[str, float | None] = {}
    for index, label in enumerate(sources):
        pan = f"c{index}" if len(sources) > 1 else "c0"
        try:
            _, text = await capture(
                [binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
                 "-af", f"pan=mono|c0={pan},aresample=48000,asetnsamples=n={SNR_WINDOW}:p=0,"
                        "astats=metadata=1:reset=1,"
                        "ametadata=print:key=lavfi.astats.Overall.RMS_level",
                 "-f", "null", "-"],
                timeout=600)
        except (Failed, OSError):
            out[label] = None
            continue
        # -inf is a window of digital zero, which the tap produces between sounds.
        # Read as the same -120 used everywhere else for it, so a tap's floor is a
        # number rather than a gap in the list.
        windows = sorted(-120.0 if "inf" in raw else float(raw)
                         for raw in RMS_LEVEL.findall(text))
        if len(windows) < 10:
            out[label] = None
            continue
        floor = windows[int(len(windows) * 0.10)]
        speech = windows[int(len(windows) * 0.90)]
        # A side with nothing in it has no ratio to report. Calling that 0 dB would
        # raise the alarm that belongs to a quiet voice, for a channel that is
        # already reported as silent by its own check.
        out[label] = None if speech <= SILENT_DB else round(speech - floor, 1)
    return out


def noisy_sides(snr: dict[str, float | None]) -> list[str]:
    """Sides whose talking is too close to their own room to transcribe well."""
    return [label for label, ratio in snr.items()
            if ratio is not None and ratio < LOW_SNR]


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
