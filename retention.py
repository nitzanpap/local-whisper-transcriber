"""Keeping only the last few recordings.

A meeting recorder that never forgets fills a disk by design. Measured on this
project, a two-source recording costs about 0.69 GB an hour, and the only thing
standing between that and a full disk was `record.disk_is_low` — which stops a
recording once there is nothing left. That is the emergency. This is the policy
that means the emergency is not reached.

Three rules make deleting somebody's meetings safe enough to do without asking
each time:

Off unless it is turned on. The default keeps everything, because an upgrade that
quietly removed recordings somebody had not finished with would be unforgivable,
and no amount of it being on the settings screen afterwards would undo it.

Only files this app wrote. The recordings folder is chosen by the user and may be
somewhere they keep other things — `~/Recordings` is a folder people already have.
A file whose name is not one this app produces is left alone however old it is.

Only the audio. The transcript is kilobytes and is the part worth keeping; the
Library already draws an entry whose media has gone, with playback disabled. So
the .m4a goes and everything read from it stays.

The file is removed rather than put in the Trash: there is no way to reach the
Trash from Python without driving Finder through AppleScript, which costs an
automation permission prompt at the worst possible moment — just after a meeting
ended. If recovery turns out to matter, that belongs in the Swift helper, which
can call FileManager.trashItem without asking anyone for anything.
"""

from __future__ import annotations

import re
from pathlib import Path

# What `record._stamp` produces, plus what `record._unique` adds to it when two
# recordings land in the same minute: "2026-08-01 14.30.m4a", "… 14.30-2.m4a",
# "… 14.30-a3f9c1.m4a". Anchored at both ends on purpose — a name that merely
# contains a date is somebody else's file.
MINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}\.\d{2}(-\w+)?\.m4a$")


def _when(path: Path) -> float:
    """When it was saved. A file that cannot be stat'd sorts oldest, and since
    the oldest are the ones deleted, `surplus` checks it exists before saying so."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def surplus(folder: Path | str, keep: int, busy: set[str] | tuple = ()) -> list[Path]:
    """Our recordings in `folder` beyond the newest `keep`, oldest last.

    `busy` is the source path of every job queued or running. Deleting the audio
    out from under a transcription that is halfway through it would fail the job
    with something unreadable about a missing file, so those are passed over and
    caught by the next recording instead.
    """
    if keep <= 0:
        return []
    try:
        mine = [p for p in Path(folder).iterdir() if MINE.match(p.name) and p.is_file()]
    except OSError:
        return []
    mine.sort(key=_when, reverse=True)   # newest first
    # By name, not by path: everything here is in one folder, and the comparison
    # failing means a file is kept that could have gone, which is the safe way
    # round for it to be wrong.
    working = {Path(source).name for source in busy}
    return [p for p in mine[keep:] if p.name not in working]


def prune(folder: Path | str, keep: int, busy: set[str] | tuple = ()) -> list[str]:
    """Delete the surplus. Returns what went, for the recording's log."""
    gone = []
    for path in surplus(folder, keep, busy):
        try:
            path.unlink()
        except OSError:
            continue    # one file that will not go is not a reason to stop
        gone.append(path.name)
    return gone
