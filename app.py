"""Local Whisper Transcriber: ffmpeg + whisper-cli behind a small local web UI.

Run: python app.py   ->  http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("LWT_DATA_DIR", Path.home() / ".local-whisper-transcriber"))
WORK_DIR = DATA_DIR / "work"
HISTORY = DATA_DIR / "history.jsonl"
SETTINGS = DATA_DIR / "settings.json"
BINARIES = ("ffmpeg", "ffprobe", "whisper-cli")
DEFAULT_EXTRA = "--temperature 0 --entropy-thold 3.0 --max-context 64"

# ponytail: one job at a time, in memory. A restart loses the in-flight run but
# never the finished files. Add a jobs table when concurrent jobs are real.
JOB: dict | None = None
PROC: asyncio.subprocess.Process | None = None

app = FastAPI(title="Local Whisper Transcriber")


class Failed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Cancelled(Exception):
    pass


# --- settings / environment -------------------------------------------------


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def binary(name: str) -> str:
    path = settings().get(f"{name}_path") or shutil.which(name)
    if not path or not os.access(path, os.X_OK):
        raise Failed("dependency_not_found", f"{name} was not found on PATH. Set its path in settings.")
    return path


MODEL_DIRS = (
    Path.home() / "whisper-models",
    Path.home() / "models",
    Path.home() / ".cache" / "whisper",
    Path.home() / "whisper.cpp" / "models",
    Path("/opt/homebrew/share/whisper-cpp"),
    Path("/usr/local/share/whisper-cpp"),
)


@lru_cache(maxsize=8)
def find_models(extra_dir: str = "") -> tuple[dict, ...]:
    """whisper.cpp models in the usual places, largest first.

    Cached per extra_dir, so saving a model from a new folder re-scans. A model
    added to a known folder mid-session needs a restart, or paste its path.
    """
    found: dict[str, int] = {}
    dirs = [*MODEL_DIRS, Path(extra_dir)] if extra_dir else list(MODEL_DIRS)
    for directory in dirs:
        try:
            for path in directory.glob("ggml-*.bin"):
                if path.is_file():
                    found[str(path.resolve())] = path.stat().st_size
        except OSError:
            continue
    return tuple(
        {"path": p, "name": Path(p).stem.removeprefix("ggml-"), "size": s}
        for p, s in sorted(found.items(), key=lambda kv: -kv[1])
    )


def environment() -> dict:
    # ponytail: existence + executable bit only. Version strings cost 3 more
    # subprocesses and tell us nothing we act on.
    out = {}
    for name in BINARIES:
        path = settings().get(f"{name}_path") or shutil.which(name)
        out[name] = {"path": path, "ok": bool(path) and os.access(path or "", os.X_OK)}
    return out


# --- subprocess helpers -----------------------------------------------------


async def capture(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    """Run to completion, return (exit code, stderr+stdout tail)."""
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise Failed("internal_error", f"{Path(cmd[0]).name} timed out")
    return p.returncode, out.decode("utf-8", "replace").strip()


async def stream(cmd: list[str], job: dict, error_code: str) -> None:
    """Run cmd, feed stderr into the job log, parse whisper progress, honour cancel."""
    global PROC
    job["log"].append("$ " + shlex.join(cmd))
    # stdout is discarded: whisper-cli prints the whole transcript there and logs
    # to stderr. Draining it to DEVNULL keeps transcript text out of the log.
    PROC = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert PROC.stderr is not None
    async for raw in PROC.stderr:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        if "progress =" in line:
            try:
                job["percent"] = float(line.split("progress =")[1].strip().rstrip("%"))
            except ValueError:
                pass
        else:
            job["log"].append(line)
    code = await PROC.wait()
    PROC = None
    if job["status"] == "cancelling":
        raise Cancelled()
    if code != 0:
        raise Failed(error_code, f"{Path(cmd[0]).name} exited with code {code}")


def kill_process_group() -> None:
    if PROC is None or PROC.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(PROC.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        PROC.terminate()  # Windows / no process groups


# --- pipeline ---------------------------------------------------------------


async def duration_seconds(path: Path) -> float | None:
    code, out = await capture(
        [binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)]
    )
    try:
        return round(float(out.splitlines()[0]), 2) if code == 0 else None
    except (ValueError, IndexError):
        return None


def make_job(source: str, model: str, out_dir: str, basename: str, *, language: str = "he",
              want_txt: bool = True, want_srt: bool = True, keep_intermediates: bool = False,
              extra_args: str = "", duration: float | None = None) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "status": "running", "stage": "starting", "percent": 0.0,
        "source": source, "model": model, "language": language,
        "out_dir": out_dir, "basename": basename,
        "want_txt": want_txt, "want_srt": want_srt,
        "keep_intermediates": keep_intermediates,
        "extra_args": shlex.split(extra_args),  # tokens, never a shell string
        "duration": duration,
        "started_at": time.time(), "ended_at": None,
        "outputs": {}, "preview": "", "error": None,
        "log": deque(maxlen=300),
    }


async def run_job(job: dict) -> None:
    work = WORK_DIR / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    try:
        job["stage"] = "converting"
        wav = work / "audio.wav"
        await stream(
            [binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", job["source"],
             "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
            job, "ffmpeg_failed",
        )

        job["stage"] = "transcribing"
        prefix = work / "out"
        cmd = [binary("whisper-cli"), "-m", job["model"], "-f", str(wav),
               "-l", job["language"], "-of", str(prefix), "--print-progress"]
        if job["want_txt"]:
            cmd.append("-otxt")
        if job["want_srt"]:
            cmd.append("-osrt")
        cmd += job["extra_args"]
        await stream(cmd, job, "whisper_failed")

        job["stage"] = "saving"
        job["percent"] = 100.0
        out_dir = Path(job["out_dir"])
        for ext, wanted in (("txt", job["want_txt"]), ("srt", job["want_srt"])):
            if not wanted:
                continue
            produced = prefix.with_suffix(f".{ext}")
            if not produced.exists():
                raise Failed("malformed_chunk_output", f"whisper-cli produced no .{ext} output")
            final = out_dir / f"{job['basename']}.{ext}"
            shutil.move(str(produced), str(final))
            job["outputs"][ext] = str(final)

        if job["want_txt"]:
            job["preview"] = read_preview(Path(job["outputs"]["txt"]))
        job["status"], job["stage"] = "completed", "completed"
    except Cancelled:
        job["status"], job["stage"] = "cancelled", "cancelled"
    except Failed as exc:
        job["status"], job["stage"] = "failed", "failed"
        job["error"] = {"code": exc.code, "message": exc.message, "details": "\n".join(list(job["log"])[-25:])}
    except Exception as exc:  # noqa: BLE001 - last resort, surfaced to the UI
        job["status"], job["stage"] = "failed", "failed"
        job["error"] = {"code": "internal_error", "message": str(exc), "details": ""}
    finally:
        job["ended_at"] = time.time()
        if not job["keep_intermediates"]:
            shutil.rmtree(work, ignore_errors=True)  # only ever the work dir
        append_history(job)


def read_preview(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def append_history(job: dict) -> None:
    row = {k: job[k] for k in ("id", "source", "model", "language", "status", "started_at", "ended_at", "outputs", "duration")}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def history(limit: int = 30) -> list[dict]:
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


# --- API --------------------------------------------------------------------


class PathIn(BaseModel):
    path: str


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
    default_language: str = "he"


def resolve_file(raw: str, what: str, code: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, {"code": code, "message": f"{what} must be an absolute path."})
    path = path.resolve()
    if not path.is_file():
        raise HTTPException(400, {"code": code, "message": f"{what} does not exist: {path}"})
    return path


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/api/state")
def state() -> dict:
    public = None
    if JOB is not None:
        public = {k: v for k, v in JOB.items() if k != "log"} | {"log": list(JOB["log"])}
    saved = settings().get("default_model_path", "")
    return {
        "environment": environment(),
        "settings": {"default_language": "he", **settings()},
        "models": find_models(str(Path(saved).parent) if saved else ""),
        "default_extra_args": DEFAULT_EXTRA,
        "job": public,
        "history": history(),
    }


@app.post("/api/inspect")
async def inspect(body: PathIn) -> dict:
    path = resolve_file(body.path, "The media file", "invalid_input_path")
    seconds = await duration_seconds(path)
    if seconds is None:
        raise HTTPException(400, {"code": "media_probe_failed",
                                  "message": "ffprobe could not read a duration from this file."})
    basename = f"{path.stem}-transcript"
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
    global JOB
    if JOB is not None and JOB["status"] in ("running", "cancelling"):
        raise HTTPException(409, {"code": "internal_error", "message": "A transcription is already running."})

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

    JOB = make_job(
        str(source), str(model), str(out_dir), basename,
        language=body.language, want_txt=body.want_txt, want_srt=body.want_srt,
        keep_intermediates=body.keep_intermediates, extra_args=body.extra_args,
        duration=await duration_seconds(source),
    )
    asyncio.create_task(run_job(JOB))
    return {"id": JOB["id"]}


@app.post("/api/cancel")
def cancel() -> dict:
    if JOB is None or JOB["status"] != "running":
        raise HTTPException(409, {"code": "cancellation_failed", "message": "No transcription is running."})
    JOB["status"] = "cancelling"
    JOB["stage"] = "cancelling"
    kill_process_group()
    return {"ok": True}


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
    folder = kind == "folder"
    prompt = "Choose the output folder" if folder else "Choose a file"
    if sys.platform == "darwin":
        verb = "choose folder" if folder else "choose file"
        # `activate` first, or the dialog can open behind the browser window and the
        # button looks dead. Bare activate targets osascript itself: no permissions.
        cmd = ["osascript", "-e", "activate", "-e", f'POSIX path of ({verb} with prompt "{prompt}")']
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={prompt}"] + (["--directory"] if folder else [])
    else:
        return {"path": None, "reason": "No native picker available on this system; paste the path instead."}
    try:
        # A picker nobody answers must not hold the request open forever.
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"path": None, "reason": "The file picker timed out. Paste the path instead."}
    if done.returncode != 0 and "-128" not in done.stderr:  # -128 is the user cancelling
        raise HTTPException(500, {"code": "internal_error",
                                  "message": "The file picker could not be opened. Paste the path instead.",
                                  "details": done.stderr.strip()})
    return {"path": done.stdout.strip() or None}


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps({k: v for k, v in body.model_dump().items() if v}, indent=2))
    return settings()


@app.delete("/api/history")
def clear_history() -> dict:
    HISTORY.unlink(missing_ok=True)  # records only; generated files are untouched
    return {"ok": True}


def sweep_work_dirs(max_age_hours: float = 6) -> None:
    """Drop scratch directories left behind by interrupted runs.

    A live job writes to its work directory continuously, so anything this old
    cannot belong to one — safe even with a second instance on another port.
    """
    cutoff = time.time() - max_age_hours * 3600
    for path in WORK_DIR.glob("*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


if __name__ == "__main__":
    import uvicorn

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    sweep_work_dirs()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LWT_PORT", 8765)))
