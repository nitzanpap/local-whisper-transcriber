# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn"]
# ///
"""Local Whisper Transcriber: ffmpeg + whisper-cli behind a small local web UI.

Run: uv run --script app.py   ->  http://127.0.0.1:8765

Routes only. The work lives in transcribe.py (media), jobs.py (queue, resume),
library.py (what has been transcribed), watch.py (folders) and tools.py (the
external binaries).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import jobs
import library
import record
import watch
from config import (BINARIES, DEFAULT_EXTRA, Failed, HISTORY, TRANSCRIPT_SUFFIX,
                    WEB_DIR, WORK_DIR, recording_config, save_settings, settings,
                    source_folders)
from tools import environment, find_models, kill_process_group, run_picker
from transcribe import duration_seconds


async def follow_parent(pid: int) -> None:
    """Exit when whatever started us goes away.

    The desktop app kills this process when it quits, but not if it is force
    quit or crashes. Without this, the app the user closed could leave a server
    behind — the exact thing they closed it to stop.
    """
    while True:
        await asyncio.sleep(5)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            os._exit(0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    jobs.sweep_work_dirs()
    jobs.restore_queue()  # pick the backlog back up after a restart
    # Nothing runs on a timer. Source folders are looked at when the app asks.
    background = []
    parent = os.environ.get("LWT_PARENT_PID")
    if parent and parent.isdigit():
        background.append(asyncio.create_task(follow_parent(int(parent))))
    yield
    for task in background:
        task.cancel()


BACKUP_KIND = "local-whisper-transcriber-settings"

app = FastAPI(title="Local Whisper Transcriber", lifespan=lifespan)


# --- request models ----------------------------------------------------------


class PathIn(BaseModel):
    path: str


class FolderIn(PathIn):
    dry_run: bool = False


class StartIn(BaseModel):
    source: str
    model: str
    language: str = "he"
    out_dir: str
    basename: str
    want_txt: bool = True
    want_srt: bool = True
    overwrite: bool = False
    keep_intermediates: bool = False
    extra_args: str = DEFAULT_EXTRA


class SettingsIn(BaseModel):
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    whisper_cli_path: str = ""
    default_model_path: str = ""
    default_language: str = ""
    default_extra_args: str = ""
    vad_model_path: str = ""
    vocabulary: str = ""
    output_folder: str = ""
    source_folders: list[str] | None = None
    watch_folders: list[str] | None = None  # what source_folders used to be called
    recording_folder: str = ""
    record_voice_device: str = ""
    record_computer_device: str = ""
    record_label_voice: str = ""
    record_label_computer: str = ""
    record_auto_transcribe: bool | None = None
    record_max_minutes: int | None = None


def resolve_file(raw: str, what: str, code: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, {"code": code, "message": f"{what} must be an absolute path."})
    path = path.resolve()
    if not path.is_file():
        raise HTTPException(400, {"code": code, "message": f"{what} was not found at {path}."})
    return path


def safe_id(job_id: str) -> str:
    if Path(job_id).name != job_id:
        raise HTTPException(400, {"code": "invalid_input_path", "message": "That is not a run this app knows about."})
    return job_id


# --- state -------------------------------------------------------------------


@app.get("/api/state")
def state() -> dict:
    public = None
    if jobs.JOB is not None:
        rows = jobs.history()
        public = {k: v for k, v in jobs.JOB.items() if k != "log"} | {
            "log": list(jobs.JOB["log"]),
            # The two things the wait was missing: how much longer, and some sign
            # that anything is happening at all.
            "remaining": jobs.estimate_remaining(jobs.JOB, rows),
            "heard": jobs.heard_so_far(jobs.JOB),
        }
    conf = settings()
    saved = conf.get("default_model_path", "")
    return {
        "environment": environment(),
        "settings": {"default_language": "auto", "default_extra_args": DEFAULT_EXTRA,
                     "output_folder": "", **conf, "source_folders": source_folders()},
        "models": find_models(str(Path(saved).parent) if saved else ""),
        "resumable": jobs.resumable(),
        "default_extra_args": DEFAULT_EXTRA,
        "job": public,
        # Cheap on purpose: a stat and a glob. Device listing is its own route.
        "recording": record.public(),
        "orphan_recordings": record.orphans(),
        "queue": [{"id": j["id"], "source": j["source"], "basename": j["basename"],
                   "language": j["language"]} for j in jobs.QUEUE],
        "history": jobs.history(),
    }


# --- starting work -----------------------------------------------------------


@app.post("/api/inspect")
async def inspect(body: PathIn) -> dict:
    path = resolve_file(body.path, "The media file", "invalid_input_path")
    seconds = await duration_seconds(path)
    if seconds is None:
        raise HTTPException(400, {"code": "media_probe_failed",
                                  "message": "This file could not be read as audio or video. If it plays elsewhere, it may be a format this copy of ffmpeg was not built for."})
    basename = f"{path.stem}{TRANSCRIPT_SUFFIX}"
    out_dir = Path(watch.output_folder_for(path))
    existing = [
        str(out_dir / f"{basename}.{ext}")
        for ext in ("txt", "srt")
        if (out_dir / f"{basename}.{ext}").exists()
    ]
    return {
        "path": str(path), "name": path.name, "size": path.stat().st_size,
        "duration": seconds, "out_dir": str(out_dir), "basename": basename,
        "existing": existing,
    }


@app.post("/api/collisions")
def collisions(body: StartIn) -> dict:
    out_dir = Path(body.out_dir).expanduser()
    wanted = [ext for ext, on in (("txt", body.want_txt), ("srt", body.want_srt)) if on]
    return {"existing": [str(out_dir / f"{body.basename}.{ext}")
                         for ext in wanted if (out_dir / f"{body.basename}.{ext}").exists()]}


@app.post("/api/start")
async def start(body: StartIn) -> dict:
    source = resolve_file(body.source, "The media file", "invalid_input_path")
    model = resolve_file(body.model, "The model file", "model_not_found")
    out_dir = Path(body.out_dir).expanduser().resolve()
    if not out_dir.is_dir():
        raise HTTPException(400, {"code": "invalid_input_path", "message": f"The folder {out_dir} does not exist, so there is nowhere to write the transcript."})
    if not os.access(out_dir, os.W_OK):
        raise HTTPException(400, {"code": "insufficient_permissions", "message": f"The folder {out_dir} cannot be written to. Choose another, or change its permissions "
                                  "in Finder."})
    if not (body.want_txt or body.want_srt):
        raise HTTPException(400, {"code": "invalid_input_path", "message": "Choose at least one output format."})
    basename = Path(body.basename).name  # no traversal via the basename field
    if not basename:
        raise HTTPException(400, {"code": "invalid_input_path", "message": "The transcript needs a name to be saved under."})

    existing = collisions(body.model_copy(update={"basename": basename, "out_dir": str(out_dir)}))["existing"]
    if existing and not body.overwrite:
        raise HTTPException(409, {"code": "output_collision",
                                  "message": "Files with this output name already exist.",
                                  "details": "\n".join(existing)})
    for name in BINARIES:
        if not environment()[name]["ok"]:
            raise HTTPException(400, {"code": "dependency_not_found",
                                      "message": f"{name} is not installed, or this app cannot find it. Its location can be set under Settings, Advanced, Expert."})

    queued = jobs.make_job(
        str(source), str(model), str(out_dir), basename,
        language=body.language, want_txt=body.want_txt, want_srt=body.want_srt,
        keep_intermediates=body.keep_intermediates, extra_args=body.extra_args,
        vad_model=settings().get("vad_model_path", ""),
        vocabulary=settings().get("vocabulary", ""),
        duration=await duration_seconds(source),
    )
    jobs.enqueue(queued)
    return {"id": queued["id"], "queued_behind": len(jobs.QUEUE) - 1}


@app.delete("/api/queue/{job_id}")
def dequeue(job_id: str) -> dict:
    """Remove a job, whether it is waiting its turn or running right now.

    It used to refuse anything but a waiting job, on the grounds that the running
    one was Cancel's business. From the queue that is a distinction without a
    difference: the button says remove, and a job that started between the page
    being drawn and the click being made is still the job the person pointed at.
    Pressing it did nothing at all and said nothing either.

    Already gone counts as done. Clicking twice, or clicking one that finished a
    moment ago, is not an error worth a message.
    """
    safe_id(job_id)
    if jobs.dequeue(job_id):
        return {"ok": True, "was": "waiting"}
    if (jobs.JOB or {}).get("id") == job_id and jobs.JOB["status"] == "running":
        jobs.JOB["status"] = "cancelling"
        jobs.JOB["stage"] = "cancelling"
        kill_process_group()
        return {"ok": True, "was": "running"}
    return {"ok": True, "was": "gone"}


@app.post("/api/resume/{job_id}")
async def resume(job_id: str) -> dict:
    safe_id(job_id)
    if any(j["id"] == job_id for j in jobs.QUEUE) or (jobs.JOB or {}).get("id") == job_id:
        raise HTTPException(409, {"code": "internal_error", "message": "That run is already queued."})
    job = jobs.load_job(job_id)
    if job is None:
        raise HTTPException(404, {"code": "invalid_input_path", "message": "That run's working files have been cleared, so there is nothing left to resume from."})
    resolve_file(job["source"], "The media file", "invalid_input_path")
    resolve_file(job["model"], "The model file", "model_not_found")
    jobs.enqueue(job)
    return {"id": job["id"], "queued_behind": len(jobs.QUEUE) - 1}


@app.delete("/api/resume/{job_id}")
def discard(job_id: str) -> dict:
    shutil.rmtree(WORK_DIR / safe_id(job_id), ignore_errors=True)  # scratch only, never outputs
    return {"ok": True}


@app.post("/api/cancel")
def cancel() -> dict:
    if jobs.JOB is None or jobs.JOB["status"] != "running":
        raise HTTPException(409, {"code": "cancellation_failed", "message": "Nothing is being transcribed, so there is nothing to cancel."})
    jobs.JOB["status"] = "cancelling"
    jobs.JOB["stage"] = "cancelling"
    kill_process_group()
    return {"ok": True}


# --- recording ---------------------------------------------------------------


class RecordIn(BaseModel):
    voice: str = ""
    computer: str = ""


@app.get("/api/record/devices")
async def record_devices() -> dict:
    """What can be recorded from, and what to install if the answer is not enough."""
    if not environment()["ffmpeg"]["ok"]:
        raise HTTPException(400, {"code": "dependency_not_found",
                                  "message": "ffmpeg is not installed, or this app cannot find it. Nothing can be recorded or "
                                  "transcribed without it."})
    try:
        return await record.devices()
    except Failed as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message, "details": ""})


@app.get("/api/record/meters")
def record_meters() -> dict:
    """The needles alone, cheap enough to ask for fifteen times a second."""
    return record.meters()


@app.post("/api/record/start")
async def record_start(body: RecordIn) -> dict:
    if not environment()["ffmpeg"]["ok"]:
        raise HTTPException(400, {"code": "dependency_not_found",
                                  "message": "ffmpeg was not found. Check Settings."})
    try:
        return await record.start(body.voice, body.computer)
    except Failed as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message, "pane": exc.pane,
                                  "details": "\n".join((record.public() or {}).get("log", [])[-12:])})


# The panes worth offering. Naming one in a sentence is not the same as getting
# somebody to it, and the difference is a person hunting through System Settings
# with a meeting already starting.
PRIVACY_PANES = {
    "audio": "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}


@app.post("/api/privacy/{which}")
def privacy_pane(which: str) -> dict:
    url = PRIVACY_PANES.get(which)
    if url is None or sys.platform != "darwin":
        raise HTTPException(400, {"code": "invalid_input_path",
                                  "message": "There is no settings pane to open for that."})
    subprocess.run(["open", url], check=False)
    return {"ok": True}


@app.post("/api/record/check")
async def record_check(body: RecordIn) -> dict:
    """Six seconds that cost nothing, so a refused permission is not discovered
    with a meeting already running."""
    try:
        return await record.check(body.voice, body.computer)
    except Failed as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message, "pane": exc.pane,
                                  "details": "\n".join((record.public() or {}).get("log", [])[-12:])})


@app.post("/api/record/stop")
async def record_stop(keep: bool = True) -> dict:
    """Finish the recording. Saving and queueing carry on in the background."""
    try:
        return await record.stop(keep=keep)
    except Failed as exc:
        raise HTTPException(409, {"code": exc.code, "message": exc.message, "details": ""})


@app.post("/api/record/dismiss")
def record_dismiss() -> dict:
    record.dismiss()
    return {"ok": True}


@app.post("/api/record/keep/{rec_id}")
async def record_keep(rec_id: str) -> dict:
    """Save audio that was captured but never written out, after a crash."""
    try:
        return await record.keep_orphan(safe_id(rec_id))
    except Failed as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message, "details": ""})


@app.delete("/api/record/keep/{rec_id}")
def record_drop(rec_id: str) -> dict:
    return record.discard_orphan(safe_id(rec_id))


# --- library -----------------------------------------------------------------


@app.get("/api/transcripts")
def transcripts() -> dict:
    return {"entries": library.entries()}


@app.get("/api/transcripts/{entry_id}")
def transcript(entry_id: str) -> dict:
    found = library.detail(safe_id(entry_id))
    if found is None:
        raise HTTPException(404, {"code": "invalid_input_path", "message": "That transcript is not in your library."})
    return found


@app.post("/api/transcripts/{entry_id}/save")
def save_transcript(entry_id: str) -> dict:
    """A copy of the text, wherever somebody wants it.

    The files are already on disk beside the recording and always were, but "open
    the folder" is an instruction to go and look rather than a way of taking
    something away. This puts it where it is wanted, once, and says where it went.

    The text is what is saved, because the reader is showing text and a question
    about formats is one nobody should be asked to answer. Subtitles are one button
    along, in the folder.
    """
    detail = library.detail(safe_id(entry_id))
    if detail is None:
        raise HTTPException(404, {"code": "not_found", "message": "That transcript is not in your library any more."})
    text = "\n".join(c["text"] for c in detail["cues"]) if detail["cues"] else detail["text"]
    if not text.strip():
        raise HTTPException(400, {"code": "no_speech_found",
                                  "message": "There is no text in that transcript to save."})
    picked = run_picker("save", "Save the transcript as",
                        f"{Path(detail['name']).stem}.txt")
    if picked.get("reason"):
        raise HTTPException(400, {"code": "internal_error", "message": picked["reason"]})
    if not picked.get("path"):
        return {"path": ""}          # cancelled, which is not a failure
    out = Path(picked["path"])
    if out.suffix.lower() != ".txt":
        out = out.with_suffix(".txt")
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(400, {"code": "insufficient_permissions",
                                  "message": f"It could not be saved there ({exc.strerror or exc}). Try somewhere else."})
    return {"path": str(out)}


@app.get("/api/media/{entry_id}")
def media(entry_id: str) -> FileResponse:
    """Stream the source audio for playback. FileResponse answers Range with 206,
    which is what makes seeking work."""
    path = library.media_path(safe_id(entry_id))
    if path is None:
        raise HTTPException(404, {"code": "invalid_input_path",
                                  "message": "The original recording is not where it was left, so there is nothing to play."})
    return FileResponse(path)


@app.get("/api/search")
def search(q: str = "") -> dict:
    return {"hits": library.search(q)}


# --- folders -----------------------------------------------------------------


@app.get("/api/pending")
def pending() -> dict:
    """What the source folders are holding. Looked at on demand, never on a timer."""
    return watch.pending()


@app.post("/api/queue-pending")
async def queue_pending() -> dict:
    return await watch.queue_pending()


@app.post("/api/queue-folder")
async def queue_folder(body: FolderIn) -> dict:
    folder = Path(body.path).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, {"code": "invalid_input_path", "message": f"{folder} is not a folder."})
    return await watch.queue_folder(folder, dry_run=body.dry_run)


# --- odds and ends -----------------------------------------------------------


@app.post("/api/reveal")
def reveal(body: PathIn) -> dict:
    path = Path(body.path).expanduser().resolve()
    if not path.exists():
        raise HTTPException(400, {"code": "invalid_input_path", "message": "That folder does not exist any more."})
    opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    subprocess.Popen([opener, str(path)])
    return {"ok": True}


@app.post("/api/pick")
def pick(kind: str = "file") -> dict:
    """Native OS picker, so the user never types a path."""
    try:
        return run_picker(kind)
    except Failed as exc:
        raise HTTPException(500, {"code": exc.code, "message": exc.message, "details": ""})


@app.get("/api/settings")
def get_settings() -> dict:
    conf = recording_config()
    return {"default_language": "auto", "default_extra_args": DEFAULT_EXTRA,
            "output_folder": "", **settings(), "source_folders": source_folders(),
            # Resolved rather than raw, so the fields show the values in force
            # instead of the blanks that mean "use the default".
            "recording_folder": conf["folder"],
            "record_label_voice": conf["labels"][0],
            "record_label_computer": conf["labels"][1],
            "record_auto_transcribe": conf["transcribe"],
            "record_max_minutes": conf["max_minutes"]}


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict:
    # Only what the client actually sent: a field left out stays as it was, and a
    # field sent empty is cleared. Without this, saving two fields from one screen
    # would blank every field on the others.
    values = body.model_dump(exclude_unset=True)
    for key in ("source_folders", "watch_folders"):
        folders = values.pop(key, None)
        if folders is not None:
            values["source_folders"] = [str(Path(f).expanduser()) for f in folders if f.strip()]
    return save_settings(values)


class BackupIn(PathIn):
    display: dict = {}


@app.post("/api/settings/export")
def export_settings(body: BackupIn) -> dict:
    """Write every setting to a file the user picked, and say where it went."""
    path = Path(body.path).expanduser()
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")
    payload = {"kind": BACKUP_KIND, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "settings": settings(), "display": body.display}
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(400, {"code": "insufficient_permissions",
                                  "message": f"The settings could not be written there ({exc.strerror or exc}). Try somewhere else."})
    return {"path": str(path)}


@app.post("/api/settings/import")
def import_settings(body: PathIn) -> dict:
    """Read a backup the user picked. Only our own files, and only small ones."""
    path = resolve_file(body.path, "The settings file", "invalid_input_path")
    if path.stat().st_size > 1_000_000:
        raise HTTPException(400, {"code": "unsupported_media",
                                  "message": "That file is far too big to be settings."})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(400, {"code": "unsupported_media",
                                  "message": "That file is not settings — it is not even JSON."})
    if not isinstance(payload, dict) or payload.get("kind") != BACKUP_KIND:
        raise HTTPException(400, {"code": "unsupported_media",
                                  "message": "That is a JSON file, but not one of ours."})
    put_settings(SettingsIn(**payload.get("settings", {})))  # same rules as saving by hand
    return {"path": str(path), "display": payload.get("display") or {}}


@app.delete("/api/history")
def clear_history() -> dict:
    HISTORY.unlink(missing_ok=True)  # records only; generated files are untouched
    return {"ok": True}


class FreshFiles(StaticFiles):
    """Serve the page, but always check whether it changed first.

    StaticFiles sends an etag and no Cache-Control, which lets a browser apply
    heuristic caching and skip revalidation — so an edited page kept showing the
    old one until a hard reload. `no-cache` means "revalidate", not "do not
    store": the etag still turns an unchanged file into a 304.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = "no-cache"
        return response


# Mounted last so /api/* wins; html=True serves index.html at /.
app.mount("/", FreshFiles(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LWT_PORT", 8765)))
