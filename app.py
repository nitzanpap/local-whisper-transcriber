"""Local Whisper Transcriber: ffmpeg + whisper-cli behind a small local web UI.

Run: python app.py   ->  http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
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

# ponytail: one job at a time, in memory, with the rest waiting in a list. A
# restart loses the queue but never a finished file, and interrupted work stays
# resumable on disk. Add a jobs table when jobs must survive a restart.
JOB: dict | None = None
QUEUE: list[dict] = []
PUMP: asyncio.Task | None = None
PROC: asyncio.subprocess.Process | None = None

@asynccontextmanager
async def lifespan(_: FastAPI):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    sweep_work_dirs()
    restore_queue()  # pick the backlog back up after a restart
    yield


app = FastAPI(title="Local Whisper Transcriber", lifespan=lifespan)


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


# Where package managers put things. launchd starts us with a minimal PATH that
# has none of them, so PATH alone is not enough to find the tools.
BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/home/linuxbrew/.linuxbrew/bin")


def locate(name: str) -> str | None:
    override = settings().get(f"{name.replace('-', '_')}_path")
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    for directory in BIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def binary(name: str) -> str:
    path = locate(name)
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
        path = locate(name)
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


async def stream(cmd: list[str], job: dict, error_code: str, capture_to: Path | None = None) -> None:
    """Run cmd, feed stderr into the job log, parse whisper progress, honour cancel.

    whisper-cli prints finished segments to stdout as it goes and logs to stderr.
    Appending stdout straight to a file keeps transcript text out of the log and
    leaves a running record to resume from if this process dies.
    """
    global PROC
    job["log"].append("$ " + shlex.join(cmd))
    sink = capture_to.open("ab") if capture_to else None
    try:
        PROC = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=sink or asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    finally:
        if sink:
            sink.close()  # the child holds its own descriptor now
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


# --- segments ---------------------------------------------------------------

# whisper-cli prints "[00:00:20.000 --> 00:00:29.980]   text" per finished segment.
SEGMENT_RE = re.compile(
    r"^\[(\d+):(\d\d):(\d\d)\.(\d{3}) --> (\d+):(\d\d):(\d\d)\.(\d{3})\]\s?(.*)$"
)


def parse_segments(path: Path) -> list[tuple[int, int, str]]:
    """Absolute (start_ms, end_ms, text) triples. Malformed lines are dropped.

    A line half-written when the process died fails the pattern, so a truncated
    file simply yields one segment fewer.
    """
    out: list[tuple[int, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = SEGMENT_RE.match(line.strip())
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2, body = m.groups()
        start = ((int(h1) * 60 + int(m1)) * 60 + int(s1)) * 1000 + int(ms1)
        end = ((int(h2) * 60 + int(m2)) * 60 + int(s2)) * 1000 + int(ms2)
        if end >= start and body.strip():
            out.append((start, end, body.strip()))
    return out


def stamp(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def write_txt(segments: list[tuple[int, int, str]], path: Path) -> None:
    path.write_text("\n".join(t for _, _, t in segments) + "\n", encoding="utf-8")


def write_srt(segments: list[tuple[int, int, str]], path: Path) -> None:
    blocks = [
        f"{i}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(segments, 1)
    ]
    # Trailing blank line terminates the last block, the way whisper's own writer
    # does; strict SRT parsers expect it. Text is stripped of whisper's leading
    # space, which is the one deliberate difference from its output.
    path.write_text("\n".join(blocks) + "\n", encoding="utf-8")


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


def save(job: dict) -> None:
    """Persist the job so a killed backend leaves something to resume from."""
    work = WORK_DIR / job["id"]
    record = {k: v for k, v in job.items() if k != "log"} | {"log": list(job["log"])[-40:]}
    try:
        work.mkdir(parents=True, exist_ok=True)
        tmp = work / "job.json.tmp"
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(work / "job.json")
    except OSError:
        pass  # a job that cannot checkpoint should still run


def stage(job: dict, name: str) -> None:
    job["stage"] = name
    save(job)


async def run_job(job: dict) -> None:
    job["status"] = "running"
    job["started_at"] = time.time()
    work = WORK_DIR / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "audio.wav"
    segments_file = work / "segments.txt"
    try:
        if wav.exists() and wav.stat().st_size > 0:
            job["log"].append(f"# reusing {wav.name} from the interrupted run")
        else:
            stage(job, "converting")
            await stream(
                [binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", job["source"],
                 "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                job, "ffmpeg_failed",
            )

        stage(job, "transcribing")
        done = parse_segments(segments_file)
        resume_ms = done[-1][1] if done else 0
        cmd = [binary("whisper-cli"), "-m", job["model"], "-f", str(wav),
               "-l", job["language"], "--print-progress"]
        if resume_ms:
            # Timestamps stay absolute across an offset, so resumed output needs
            # no stitching — it simply continues the same segment file.
            cmd += ["--offset-t", str(resume_ms)]
            job["log"].append(f"# resuming at {stamp(resume_ms)} ({len(done)} segments already done)")
        cmd += job["extra_args"]
        await stream(cmd, job, "whisper_failed", capture_to=segments_file)

        stage(job, "saving")
        job["percent"] = 100.0
        segments = parse_segments(segments_file)
        if not segments:
            raise Failed("malformed_chunk_output", "whisper-cli produced no transcript segments")
        out_dir = Path(job["out_dir"])
        writers = {"txt": write_txt, "srt": write_srt}
        for ext, wanted in (("txt", job["want_txt"]), ("srt", job["want_srt"])):
            if not wanted:
                continue
            final = out_dir / f"{job['basename']}.{ext}"
            staged = work / f"final.{ext}"
            writers[ext](segments, staged)  # write aside, then move into place
            shutil.move(str(staged), str(final))
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
        if job["status"] == "completed" and not job["keep_intermediates"]:
            shutil.rmtree(work, ignore_errors=True)  # only ever the work dir
        else:
            save(job)  # cancelled or failed: keep the audio and segments to resume from
        append_history(job)
        # Also sweep here, not just at startup: under launchd this process can run
        # for weeks, and a startup-only sweep would never fire.
        sweep_work_dirs()


async def pump() -> None:
    """Run queued jobs one at a time. Sequential on purpose: two whisper runs on
    one machine finish no sooner and fight over memory."""
    global JOB
    while QUEUE:
        JOB = QUEUE.pop(0)
        await run_job(JOB)


def enqueue(job: dict) -> None:
    global PUMP
    job["status"] = "queued"
    save(job)  # so a backlog survives a restart, not just the job in flight
    QUEUE.append(job)
    if PUMP is None or PUMP.done():
        PUMP = asyncio.create_task(pump())


def restore_queue() -> None:
    """Re-enqueue jobs that were still waiting when the process last stopped."""
    waiting = []
    for record in WORK_DIR.glob("*/job.json"):
        try:
            job = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") == "queued":
            job["log"] = deque(job.get("log", []), maxlen=300)
            waiting.append(job)
    for job in sorted(waiting, key=lambda j: j.get("started_at") or 0):
        QUEUE.append(job)
    if waiting:
        global PUMP
        PUMP = asyncio.create_task(pump())


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


def resumable() -> list[dict]:
    """Runs whose work directory outlived the process that was doing them."""
    out = []
    for record in WORK_DIR.glob("*/job.json"):
        try:
            job = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("id") == (JOB or {}).get("id"):
            continue  # the live one
        if job.get("status") not in ("running", "cancelling", "cancelled", "failed"):
            continue
        done = parse_segments(record.parent / "segments.txt")
        out.append({
            "id": job["id"], "source": job["source"], "basename": job["basename"],
            "language": job.get("language", ""), "percent": job.get("percent", 0.0),
            "reached_ms": done[-1][1] if done else 0,
            "duration": job.get("duration"),
            "was": "interrupted" if job.get("status") in ("running", "cancelling") else job["status"],
        })
    return sorted(out, key=lambda j: -j["reached_ms"])


def load_job(job_id: str) -> dict:
    record = WORK_DIR / job_id / "job.json"
    try:
        job = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, {"code": "invalid_input_path", "message": "That run is no longer on disk."})
    job["log"] = deque(job.get("log", []), maxlen=300)
    job["status"], job["percent"] = "running", 0.0
    job["error"], job["ended_at"] = None, None
    job["started_at"] = time.time()
    return job


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
        "resumable": resumable(),
        "default_extra_args": DEFAULT_EXTRA,
        "job": public,
        "queue": [{"id": j["id"], "source": j["source"], "basename": j["basename"],
                   "language": j["language"]} for j in QUEUE],
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

    queued = make_job(
        str(source), str(model), str(out_dir), basename,
        language=body.language, want_txt=body.want_txt, want_srt=body.want_srt,
        keep_intermediates=body.keep_intermediates, extra_args=body.extra_args,
        duration=await duration_seconds(source),
    )
    enqueue(queued)
    return {"id": queued["id"], "queued_behind": len(QUEUE) - 1}


@app.delete("/api/queue/{job_id}")
def dequeue(job_id: str) -> dict:
    """Drop a job that has not started. The running one is Cancel's business."""
    before = len(QUEUE)
    QUEUE[:] = [j for j in QUEUE if j["id"] != job_id]
    if len(QUEUE) == before:
        raise HTTPException(404, {"code": "invalid_input_path", "message": "That job is not waiting any more."})
    # Drop the checkpoint too, or a restart would bring the job back from the dead.
    shutil.rmtree(WORK_DIR / Path(job_id).name, ignore_errors=True)
    return {"ok": True}


@app.post("/api/resume/{job_id}")
async def resume(job_id: str) -> dict:
    if Path(job_id).name != job_id:
        raise HTTPException(400, {"code": "invalid_input_path", "message": "Bad run id."})
    if any(j["id"] == job_id for j in QUEUE) or (JOB or {}).get("id") == job_id:
        raise HTTPException(409, {"code": "internal_error", "message": "That run is already queued."})
    job = load_job(job_id)
    resolve_file(job["source"], "The media file", "invalid_input_path")
    resolve_file(job["model"], "The model file", "model_not_found")
    enqueue(job)
    return {"id": job["id"], "queued_behind": len(QUEUE) - 1}


@app.delete("/api/resume/{job_id}")
def discard(job_id: str) -> dict:
    if Path(job_id).name != job_id:
        raise HTTPException(400, {"code": "invalid_input_path", "message": "Bad run id."})
    shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)  # scratch only, never outputs
    return {"ok": True}


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
    many = kind == "files"
    prompt = "Choose the output folder" if folder else "Choose audio or video files"
    if sys.platform == "darwin":
        verb = "choose folder" if folder else "choose file"
        script = (
            'set picked to {verb} with prompt "{prompt}"{multi}\n'
            'set out to ""\n'
            "repeat with one in (picked as list)\n"
            "  set out to out & POSIX path of one & linefeed\n"
            "end repeat\n"
            "return out"
        ).format(verb=verb, prompt=prompt, multi=" with multiple selections allowed" if many else "")
        # `activate` first, or the dialog can open behind the browser window and the
        # button looks dead. Bare activate targets osascript itself: no permissions.
        cmd = ["osascript", "-e", "activate", "-e", script]
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={prompt}", "--separator=\n"]
        cmd += ["--directory"] if folder else (["--multiple"] if many else [])
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
    paths = [line for line in done.stdout.splitlines() if line.strip()]
    return {"path": paths[0] if paths else None, "paths": paths}


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps({k: v for k, v in body.model_dump().items() if v}, indent=2))
    return settings()


@app.delete("/api/history")
def clear_history() -> dict:
    HISTORY.unlink(missing_ok=True)  # records only; generated files are untouched
    return {"ok": True}


def sweep_work_dirs(max_age_hours: float = 6, keep_resumable_days: float = 7) -> None:
    """Drop stale scratch directories.

    A live job writes to its work directory continuously, so anything this old
    cannot belong to one — safe even with a second instance on another port.
    Anything still resumable gets a much longer reprieve; that WAV and segment
    file are the only way to avoid re-doing the work.
    """
    now = time.time()
    # Queued jobs are spared too: a long backlog can outlive the short limit while
    # its checkpoint is still the only record that the job was ever asked for.
    keeping = {j["id"] for j in resumable()} | {j["id"] for j in QUEUE}
    for path in WORK_DIR.glob("*"):
        try:
            if not path.is_dir():
                continue
            limit = keep_resumable_days * 86400 if path.name in keeping else max_age_hours * 3600
            if now - path.stat().st_mtime > limit:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


if __name__ == "__main__":
    import uvicorn

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    sweep_work_dirs()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LWT_PORT", 8765)))
