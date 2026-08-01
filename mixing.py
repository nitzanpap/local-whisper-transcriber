"""The ffmpeg commands: what to record with, and how to put it together after.

One rule runs through all of it, and it was expensive to learn. Nothing is mixed
while it is being recorded. Both sources used to arrive as two live inputs of one
ffmpeg, joined as they came, which meant reconciling two independent clocks in
real time — and `aresample` filled the difference with silence, measured at
0.237 s of it nearly four times a second. That left the louder side intact and
destroyed the quieter one, so a voice came back in pieces too broken for VAD to
consider speech.

So each source is captured alone, into its own file, and combined afterwards from
finished files where there are no clocks left to chase. Everything here is a pure
function of a recording dict: it builds a command and returns it, and running one
is somebody else's job.
"""

from __future__ import annotations

import sys
from pathlib import Path

from levels import EMPTY_WAV, METERS
from syshelper import SYSTEM_AUDIO
from tools import binary

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
        if helper_takes(rec, side):
            continue
        out.append(capture_command(rec, device, path))
    return out


def helper_takes(rec: dict, side: str) -> bool:
    """Whether this side is the helper's job rather than ffmpeg's.

    Any real input device, either side. A loopback driver picked as the computer's
    side — Teams, Zoom — is an input device like any other, and it was going
    through ffmpeg and losing an eighth of its samples long after the microphone
    stopped doing so.
    """
    chosen = rec.get(side)
    return bool(rec.get("helper")) and bool(chosen) and chosen != SYSTEM_AUDIO


def helper_takes_the_microphone(rec: dict) -> bool:
    return helper_takes(rec, "voice")


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
