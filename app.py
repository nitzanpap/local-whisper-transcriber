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
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import jobs
import library
import watch
from config import (BINARIES, DEFAULT_EXTRA, Failed, HISTORY, TRANSCRIPT_SUFFIX,
                    WEB_DIR, WORK_DIR, save_settings, settings)
from tools import environment, find_models, kill_process_group, run_picker
from transcribe import duration_seconds


@asynccontextmanager
async def lifespan(_: FastAPI):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    jobs.sweep_work_dirs()
    jobs.restore_queue()  # pick the backlog back up after a restart
    watcher = asyncio.create_task(watch.watcher())
    yield
    watcher.cancel()


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
    watch_folders: list[str] | None = None


def resolve_file(raw: str, what: str, code: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, {"code": code, "message": f"{what} must be an absolute path."})
    path = path.resolve()
    if not path.is_file():
        raise HTTPException(400, {"code": code, "message": f"{what} does not exist: {path}"})
    return path


def safe_id(job_id: str) -> str:
    if Path(job_id).name != job_id:
        raise HTTPException(400, {"code": "invalid_input_path", "message": "Bad run id."})
    return job_id


# --- state -------------------------------------------------------------------


@app.get("/api/state")
def state() -> dict:
    public = None
    if jobs.JOB is not None:
        public = {k: v for k, v in jobs.JOB.items() if k != "log"} | {"log": list(jobs.JOB["log"])}
    conf = settings()
    saved = conf.get("default_model_path", "")
    return {
        "environment": environment(),
        "settings": {"default_language": "he", "default_extra_args": DEFAULT_EXTRA,
                     "watch_folders": [], **conf},
        "models": find_models(str(Path(saved).parent) if saved else ""),
        "resumable": jobs.resumable(),
        "default_extra_args": DEFAULT_EXTRA,
        "job": public,
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
                                  "message": "ffprobe could not read a duration from this file."})
    basename = f"{path.stem}{TRANSCRIPT_SUFFIX}"
    existing = [
        str(path.parent / f"{basename}.{ext}")
        for ext in ("txt", "srt")
        if (path.parent / f"{basename}.{ext}").exists()
    ]
    return {
        "path": str(path), "name": path.name, "size": path.stat().st_size,
        "duration": seconds, "out_dir": str(path.parent), "basename": basename,
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
        raise HTTPException(400, {"code": "invalid_input_path", "message": f"Output folder does not exist: {out_dir}"})
    if not os.access(out_dir, os.W_OK):
        raise HTTPException(400, {"code": "insufficient_permissions", "message": f"Output folder is not writable: {out_dir}"})
    if not (body.want_txt or body.want_srt):
        raise HTTPException(400, {"code": "invalid_input_path", "message": "Choose at least one output format."})
    basename = Path(body.basename).name  # no traversal via the basename field
    if not basename:
        raise HTTPException(400, {"code": "invalid_input_path", "message": "Output name is required."})

    existing = collisions(body.model_copy(update={"basename": basename, "out_dir": str(out_dir)}))["existing"]
    if existing and not body.overwrite:
        raise HTTPException(409, {"code": "output_collision",
                                  "message": "Files with this output name already exist.",
                                  "details": "\n".join(existing)})
    for name in BINARIES:
        if not environment()[name]["ok"]:
            raise HTTPException(400, {"code": "dependency_not_found",
                                      "message": f"{name} was not found. Check Settings."})

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
    """Drop a job that has not started. The running one is Cancel's business."""
    if not jobs.dequeue(safe_id(job_id)):
        raise HTTPException(404, {"code": "invalid_input_path", "message": "That job is not waiting any more."})
    return {"ok": True}


@app.post("/api/resume/{job_id}")
async def resume(job_id: str) -> dict:
    safe_id(job_id)
    if any(j["id"] == job_id for j in jobs.QUEUE) or (jobs.JOB or {}).get("id") == job_id:
        raise HTTPException(409, {"code": "internal_error", "message": "That run is already queued."})
    job = jobs.load_job(job_id)
    if job is None:
        raise HTTPException(404, {"code": "invalid_input_path", "message": "That run is no longer on disk."})
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
        raise HTTPException(409, {"code": "cancellation_failed", "message": "No transcription is running."})
    jobs.JOB["status"] = "cancelling"
    jobs.JOB["stage"] = "cancelling"
    kill_process_group()
    return {"ok": True}


# --- library -----------------------------------------------------------------


@app.get("/api/transcripts")
def transcripts() -> dict:
    return {"entries": library.entries()}


@app.get("/api/transcripts/{entry_id}")
def transcript(entry_id: str) -> dict:
    found = library.detail(safe_id(entry_id))
    if found is None:
        raise HTTPException(404, {"code": "invalid_input_path", "message": "No such transcript."})
    return found


@app.get("/api/media/{entry_id}")
def media(entry_id: str) -> FileResponse:
    """Stream the source audio for playback. FileResponse answers Range with 206,
    which is what makes seeking work."""
    path = library.media_path(safe_id(entry_id))
    if path is None:
        raise HTTPException(404, {"code": "invalid_input_path",
                                  "message": "The original recording is no longer where it was."})
    return FileResponse(path)


@app.get("/api/search")
def search(q: str = "") -> dict:
    return {"hits": library.search(q)}


# --- folders -----------------------------------------------------------------


@app.post("/api/queue-folder")
async def queue_folder(body: FolderIn) -> dict:
    folder = Path(body.path).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, {"code": "invalid_input_path", "message": f"Not a folder: {folder}"})
    return await watch.queue_folder(folder, dry_run=body.dry_run)


# --- odds and ends -----------------------------------------------------------


@app.post("/api/reveal")
def reveal(body: PathIn) -> dict:
    path = Path(body.path).expanduser().resolve()
    if not path.exists():
        raise HTTPException(400, {"code": "invalid_input_path", "message": "That folder no longer exists."})
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
    return {"default_language": "he", "default_extra_args": DEFAULT_EXTRA,
            "watch_folders": [], **settings()}


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict:
    # Only what the client actually sent: a field left out stays as it was, and a
    # field sent empty is cleared. Without this, saving two fields from one screen
    # would blank every field on the others.
    values = body.model_dump(exclude_unset=True)
    folders = values.pop("watch_folders", None)
    if folders is not None:
        values["watch_folders"] = [str(Path(f).expanduser()) for f in folders if f.strip()]
    return save_settings(values)


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
