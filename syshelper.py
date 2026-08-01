"""The Swift helper that captures this Mac's own audio, and how to talk to it.

macOS has no system-audio input device. A loopback driver — BlackHole and friends
— provides one, and asking somebody to install a kernel extension before they can
record a meeting is not a product. So this app ships its own capture helper, which
uses a Core Audio process tap: the thing the separate "System Audio Recording
Only" permission exists for, with no screen recording and no driver involved.

Everything here is the boundary with that helper: where it lives, how it gets
built, and the two things it says on the way out — an exit code for a refused
permission, and a level line once a tenth of a second.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from config import DATA_DIR, Failed
from tools import capture

# The helper's own exit code for a permission macOS would not give. Kept in step
# with DENIED in mac/syscapture.swift.
HELPER_DENIED = 3


# The helper's own meter, once a second, so the computer's side has one too.
# The helper captures both sides now, so it says which one it is speaking for.
HELPER_LEVEL = re.compile(r"syscapture: (\w+) level (-?\d+(?:\.\d+)?) frames (\d+)")
# Digital zero, which the helper reports as -120 and no real signal ever reaches.
# Frames arriving with nothing in them at all is what a refused tap looks like;
# frames not arriving is only a quiet machine.
DEAD_TAP = -100.0


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
