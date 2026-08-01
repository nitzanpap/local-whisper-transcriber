"""Getting a transcription model, without leaving the app.

The catalogue says what exists; this fetches one and proves it arrived intact.

Downloads resume. A model is a gigabyte or three, and somebody who loses their
connection at 80% should not start again — so the bytes go to a `.part` file and
the next attempt asks the server to carry on from wherever that got to. Cancelling
is the same thing said deliberately: the part stays, and pressing download again
picks it up.

Nothing is trusted until it is verified. A truncated file and a finished one look
identical on disk, and a half a model produces a whisper-cli failure nobody could
diagnose — so the sha256 in the catalogue is checked before the file is put in
place under its real name.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from config import Failed
from tools import catalogue, find_models

# Where a model goes when nothing says otherwise.
#
# Not "whisper-models", though that is still searched and always will be. The
# engine behind this may not be whisper for ever — Parakeet and the rest are
# better at some of this already — and a folder named after today's engine is a
# migration waiting to happen. It is a plain visible folder rather than something
# inside the app, so a model downloaded here can be used by whisper.cpp on the
# command line, or by anything else, without being copied out.
HOME = Path.home() / "speech-models"

# What is happening right now, or None. One at a time on purpose: two gigabyte
# downloads over one connection finish later than the same two in turn, and the
# interface has one thing to say instead of a list.
BUSY: dict | None = None
TASK: asyncio.Task | None = None


def catalogued() -> list[dict]:
    """Every model this app knows of, and whether it is already here.

    Two lists in one, because that is the shape of the question: what can I use
    now, and what could I have.
    """
    here = {Path(m["path"]).name: m["path"] for m in find_models()}
    out = []
    for filename, said in catalogue().items():
        path = here.get(filename, "")
        out.append({**said, "path": path, "have": bool(path),
                    "downloading": bool(BUSY and BUSY["id"] == said["id"])})
    out.sort(key=lambda m: (not m["have"], m["rank"]))
    return out


def public() -> dict | None:
    """What the download looks like from outside, for the once-a-second poll."""
    if BUSY is None:
        return None
    done, total = BUSY["done"], BUSY["total"]
    return {"id": BUSY["id"], "name": BUSY["name"], "done": done, "total": total,
            "percent": round(done * 100 / total, 1) if total else 0.0,
            "status": BUSY["status"], "error": BUSY["error"]}


def _entry(model_id: str) -> dict:
    for said in catalogue().values():
        if said["id"] == model_id:
            return said
    raise Failed("model_not_found", f"There is no model called {model_id} in the catalogue.")


def _room_for(size: int, where: Path) -> None:
    try:
        free = shutil.disk_usage(where).free
    except OSError:
        return  # unreadable is not a reason to refuse
    # The file itself, and the same again for the moment it is renamed into place.
    if free < size + 200_000_000:
        raise Failed("insufficient_disk",
                     f"This model needs {size / 1e9:.1f} GB and there is "
                     f"{free / 1e9:.1f} GB free. Make some room and try again.")


def _fetch(said: dict, part: Path, target: Path) -> None:
    """The blocking half: bytes onto disk, resuming whatever is already there."""
    have = part.stat().st_size if part.exists() else 0
    if have > said["size_bytes"]:
        part.unlink()  # a part longer than the whole file is not a prefix of it
        have = 0
    request = urllib.request.Request(said["url"])
    if have:
        request.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(request, timeout=60) as answer:
        # A server that ignores the range hands back the whole file from zero, and
        # appending that to what we have would make nonsense. Start again instead.
        if have and answer.status != 206:
            have = 0
            part.unlink(missing_ok=True)
        BUSY["done"] = have
        with part.open("ab" if have else "wb") as sink:
            while True:
                if BUSY["status"] == "cancelling":
                    raise Cancelled()
                chunk = answer.read(1 << 20)
                if not chunk:
                    break
                sink.write(chunk)
                BUSY["done"] += len(chunk)

    BUSY["status"] = "checking"
    digest = hashlib.sha256()
    with part.open("rb") as reading:
        for block in iter(lambda: reading.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != said["sha256"]:
        part.unlink(missing_ok=True)
        raise Failed("download_failed",
                     "The downloaded file is not the one the catalogue describes, so it "
                     "was thrown away rather than used. Try again.")
    part.replace(target)


class Cancelled(Exception):
    pass


async def download(model_id: str) -> dict:
    """Fetch a model, or carry on fetching one that was interrupted."""
    global BUSY, TASK
    if BUSY is not None and BUSY["status"] in ("downloading", "checking"):
        raise Failed("already_recording", "One model is already being downloaded.")
    said = _entry(model_id)
    HOME.mkdir(parents=True, exist_ok=True)
    target = HOME / said["filename"]
    if target.is_file():
        return {"ok": True, "path": str(target)}
    _room_for(said["size_bytes"], HOME)
    BUSY = {"id": said["id"], "name": said["name"], "done": 0,
            "total": said["size_bytes"], "status": "downloading", "error": None}
    part = HOME / (said["filename"] + ".part")

    async def run() -> None:
        global BUSY
        try:
            await asyncio.to_thread(_fetch, said, part, target)
        except Cancelled:
            # The part stays where it is. Asking again carries on from here, which
            # is the whole reason cancelling is not the same as throwing it away.
            BUSY = None
            return
        except Failed as exc:
            BUSY = {**BUSY, "status": "failed", "error": exc.message}
            return
        except (OSError, urllib.error.URLError, ValueError) as exc:
            BUSY = {**BUSY, "status": "failed",
                    "error": f"The download did not finish: {exc}. What arrived is kept, "
                             "so asking again carries on rather than starting over."}
            return
        find_models.cache_clear()   # it is on disk now; the scan has to see it
        BUSY = None

    TASK = asyncio.create_task(run())
    return public() or {"ok": True}


def cancel() -> dict:
    """Stop, keeping what has arrived."""
    if BUSY is None:
        return {"ok": True}
    BUSY["status"] = "cancelling"
    return {"ok": True}


def forget(model_id: str) -> dict:
    """Delete a model from disk, and any half of one beside it."""
    said = _entry(model_id)
    gone = False
    for m in find_models():
        if Path(m["path"]).name == said["filename"]:
            Path(m["path"]).unlink(missing_ok=True)
            gone = True
    (HOME / (said["filename"] + ".part")).unlink(missing_ok=True)
    find_models.cache_clear()
    return {"ok": True, "deleted": gone}


def rescan() -> dict:
    """Look again. The scan is cached, so a model put there by hand needs asking.

    This is why the README used to tell people to restart the app after putting a
    file in the folder, which is a strange thing to have to say.
    """
    find_models.cache_clear()
    return {"models": catalogued()}
