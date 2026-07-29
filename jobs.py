"""Job lifecycle: the queue, checkpoints on disk, resume, history, scratch sweep.

One job runs at a time and the rest wait in QUEUE. Every job is checkpointed to
its work directory, so a killed process leaves both the backlog and the
half-finished run recoverable.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import time
import uuid
from collections import deque
from pathlib import Path

from config import Cancelled, Failed, HISTORY, WORK_DIR, DATA_DIR
from transcribe import parse_segments, stamp, to_wav, transcribe, write_outputs

JOB: dict | None = None
QUEUE: list[dict] = []
PUMP: asyncio.Task | None = None

RESUMABLE_STATES = ("running", "cancelling", "cancelled", "failed")


def make_job(source: str, model: str, out_dir: str, basename: str, *, language: str = "he",
             want_txt: bool = True, want_srt: bool = True, keep_intermediates: bool = False,
             extra_args: str = "", duration: float | None = None, vad_model: str = "",
             vocabulary: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "status": "running", "stage": "starting", "percent": 0.0,
        "source": source, "model": model, "language": language,
        "out_dir": out_dir, "basename": basename,
        "want_txt": want_txt, "want_srt": want_srt,
        "keep_intermediates": keep_intermediates,
        "extra_args": shlex.split(extra_args),  # tokens, never a shell string
        "vad_model": vad_model,
        "vocabulary": vocabulary,
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


def read_preview(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


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
            await to_wav(job, job["source"], wav)

        stage(job, "transcribing")
        done = parse_segments(segments_file)
        resume_ms = done[-1][1] if done else 0
        if resume_ms:
            job["log"].append(f"# resuming at {stamp(resume_ms)} ({len(done)} segments already done)")
        await transcribe(job, wav, segments_file, resume_ms)

        stage(job, "saving")
        job["percent"] = 100.0
        write_outputs(job, work, parse_segments(segments_file))
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
    global PUMP
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
        PUMP = asyncio.create_task(pump())


def dequeue(job_id: str) -> bool:
    before = len(QUEUE)
    QUEUE[:] = [j for j in QUEUE if j["id"] != job_id]
    if len(QUEUE) == before:
        return False
    # Drop the checkpoint too, or a restart would bring the job back from the dead.
    shutil.rmtree(WORK_DIR / Path(job_id).name, ignore_errors=True)
    return True


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
        if job.get("status") not in RESUMABLE_STATES:
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


def load_job(job_id: str) -> dict | None:
    record = WORK_DIR / job_id / "job.json"
    try:
        job = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    job["log"] = deque(job.get("log", []), maxlen=300)
    job["status"], job["percent"] = "running", 0.0
    job["error"], job["ended_at"] = None, None
    job["started_at"] = time.time()
    return job


def append_history(job: dict) -> None:
    row = {k: job[k] for k in ("id", "source", "model", "language", "status",
                               "started_at", "ended_at", "outputs", "duration")}
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
