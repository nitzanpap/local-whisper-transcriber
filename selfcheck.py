"""Six seconds that answer the question a recording cannot ask afterwards.

Nothing can tell a refused tap from a machine that happens to be quiet, because
both deliver nothing at all. So the machine is made not quiet: a tone of the
app's own is played through whatever the computer is playing out of, and if the
tap cannot hear that, the silence is not the room's.

Whether the tone actually played is half the verdict, which is why
`play_test_tone` returns a bool rather than nothing. A tap that heard nothing
while a sound was playing was refused. A tap that heard nothing because nothing
played has told us only that nothing played.
"""

from __future__ import annotations

import time

from config import Failed, save_settings
from syshelper import DEAD_TAP
from levels import SILENT_LUFS
from tools import binary, capture

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


async def play_test_tone(rec: dict) -> bool:
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
