"""Finding the external tools and running them.

Everything that shells out lives here: argv arrays only, never a shell string.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from config import BINARIES, Cancelled, Failed, settings

# Where package managers put things. launchd starts us with a minimal PATH that
# has none of them, so PATH alone is not enough to find the tools.
BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/home/linuxbrew/.linuxbrew/bin")

MODEL_DIRS = (
    Path.home() / "whisper-models",
    Path.home() / "models",
    Path.home() / ".cache" / "whisper",
    Path.home() / "whisper.cpp" / "models",
    Path("/opt/homebrew/share/whisper-cpp"),
    Path("/usr/local/share/whisper-cpp"),
)

# The child process of the moment, so cancel can reach it.
PROC: asyncio.subprocess.Process | None = None

# Where whisper says it found speech, in the original file's own timeline:
#
#   whisper_vad: vad_segment_info: orig_start: 0.96, orig_end: 1.44, vad_start: 0.00, vad_end: 0.48
#
# These are the measured truth about when somebody spoke, and they were being
# thrown away as noise. They still stay out of the job log — thousands of them on
# a long recording would push the command itself out of it — but they are written
# to a file now, because the timestamps in the transcript are built from them.
REGION_LINE = re.compile(
    r"^whisper_vad:\s+vad_segment_info:\s+"
    r"orig_start:\s*(-?\d+\.\d+),\s+orig_end:\s*(-?\d+\.\d+),\s+"
    r"vad_start:\s*(-?\d+\.\d+),\s+vad_end:\s*(-?\d+\.\d+)\s*$")


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


def environment() -> dict:
    # ponytail: existence + executable bit only. Version strings cost 3 more
    # subprocesses and tell us nothing we act on.
    out = {}
    for name in BINARIES:
        path = locate(name)
        out[name] = {"path": path, "ok": bool(path) and os.access(path or "", os.X_OK)}
    return out


# A VAD model is a ggml-*.bin sitting in the same folder, but it cannot transcribe
# anything, so it must never be offered as a choice of model.
NOT_A_TRANSCRIPTION_MODEL = ("silero", "vad")


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
                if any(hint in path.stem.lower() for hint in NOT_A_TRANSCRIPTION_MODEL):
                    continue
                if path.is_file():
                    found[str(path.resolve())] = path.stat().st_size
        except OSError:
            continue
    return tuple(
        {"path": p, "name": Path(p).stem.removeprefix("ggml-"), "size": s}
        for p, s in sorted(found.items(), key=lambda kv: -kv[1])
    )


async def capture(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    """Run to completion, return (exit code, stderr+stdout tail)."""
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise Failed("internal_error",
                     f"{Path(cmd[0]).name} was still running after {timeout:.0f} seconds "
                     "and was stopped. Something it needed is probably not answering.")
    return p.returncode, out.decode("utf-8", "replace").strip()


# What the model decided the language actually is, which is only ever printed when
# it was allowed to decide — "auto-detected language: he (p = 0.98)". The confidence
# comes with it because a low one is the answer to "why is my transcript nonsense".
DETECTED = re.compile(r"auto-detected language:\s*([a-z]{2,3})\s*\(p\s*=\s*([\d.]+)\)")


async def stream(cmd: list[str], job: dict, error_code: str, capture_to: Path | None = None,
                 regions_to: Path | None = None) -> None:
    """Run cmd, feed stderr into the job log, parse whisper progress, honour cancel.

    whisper-cli prints finished segments to stdout as it goes and logs to stderr.
    Appending stdout straight to a file keeps transcript text out of the log and
    leaves a running record to resume from if this process dies.
    """
    global PROC
    job["log"].append("$ " + shlex.join(cmd))
    sink = capture_to.open("ab") if capture_to else None
    regions = regions_to.open("ab") if regions_to else None
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
    sampler = asyncio.create_task(watch_memory(PROC.pid, job))
    assert PROC.stderr is not None
    async for raw in PROC.stderr:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        if REGION_LINE.match(line):
            # Kept, but not in the log: one of these per pause in the conversation
            # would bury the command line and everything else worth reading.
            if regions is not None:
                regions.write(line.encode("utf-8") + b"\n")
                regions.flush()
            continue
        found = DETECTED.search(line)
        if found:
            # The first track to say so speaks for the recording: both sides of one
            # conversation are the same language, and a second guess on a quieter
            # channel is a worse guess rather than a second opinion.
            job.setdefault("detected_language", found.group(1))
            job.setdefault("language_confidence", round(float(found.group(2)), 3))
        if "progress =" in line:
            try:
                reported = float(line.split("progress =")[1].strip().rstrip("%"))
            except ValueError:
                pass
            else:
                # whisper counts from nought on every run, so a job made of two
                # tracks gives each one a slice of the bar. One track is the whole
                # bar, which is the arithmetic doing nothing.
                base = job.get("percent_base") or 0.0
                span = job.get("percent_span") or 100.0
                job["percent"] = base + reported * span / 100.0
        else:
            job["log"].append(line)
    code = await PROC.wait()
    if regions is not None:
        regions.close()
    sampler.cancel()
    PROC = None
    if job["status"] == "cancelling":
        raise Cancelled()
    if code != 0:
        # The last thing it said, which is nearly always the actual reason — a process
        # exiting with 1 tells nobody anything on its own. Ahead of the code, and in
        # the middle of the sentence rather than trailing off it.
        last = next((line.strip().rstrip(".") for line in reversed(list(job["log"])[-12:])
                     if line.strip() and not line.startswith("$")), "")
        raise Failed(error_code,
                     f"{Path(cmd[0]).name} stopped with an error, so the transcription "
                     f"could not finish. The last thing it said was: {last or 'nothing'}. "
                     "There is more in the process log below.")


async def watch_memory(pid: int, job: dict, every: float = 2.0) -> None:
    """Follow the child's memory so a finished job can say what it cost.

    Sampled rather than measured: ru_maxrss for children is a high-water mark
    across every child ever waited for, so it cannot answer "this job".
    """
    while True:
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if out:
                job["peak_memory_mb"] = max(job.get("peak_memory_mb") or 0, round(int(out) / 1024))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        await asyncio.sleep(every)


def kill_process_group() -> None:
    if PROC is None or PROC.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(PROC.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        PROC.terminate()  # Windows / no process groups


BACKUP_NAME = "local-whisper-transcriber-settings.json"


def picker_command(kind: str, prompt: str = "", name: str = "") -> tuple[list[str] | None, str]:
    """The native file/folder/save picker for this OS, or (None, why not).

    `prompt` and `name` are for the Save panel, which is used for more than one
    thing now: a settings backup, and a copy of a transcript put wherever somebody
    wants it. Quotes are stripped rather than escaped — these end up inside an
    AppleScript string literal, and a transcript is named after a file somebody else
    chose the name of.
    """
    folder = kind == "folder"
    many = kind == "files"
    saving = kind == "save"
    clean = lambda text: text.replace('"', "").replace("\\", "")
    prompt = clean(prompt) or ("Save your settings as" if saving else
                               "Choose the output folder" if folder else
                               "Choose audio or video files")
    if sys.platform == "darwin":
        if saving:
            # `choose file name` is the Save panel: the user picks the place, so
            # afterwards the app can say exactly where the file went.
            return ["osascript", "-e", "activate", "-e",
                    f'POSIX path of (choose file name with prompt "{prompt}" '
                    f'default name "{clean(name) or BACKUP_NAME}")'], ""
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
        return ["osascript", "-e", "activate", "-e", script], ""
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={prompt}", "--separator=\n"]
        if saving:
            # The name that was asked for, like the macOS branch above. This said
            # BACKUP_NAME whatever it was saving, so somebody keeping a copy of a
            # transcript was offered it under the name of the settings file.
            cmd += ["--save", "--confirm-overwrite", f"--filename={clean(name) or BACKUP_NAME}"]
        else:
            cmd += ["--directory"] if folder else (["--multiple"] if many else [])
        return cmd, ""
    return None, "No native picker available on this system; paste the path instead."


def run_picker(kind: str, prompt: str = "", name: str = "") -> dict:
    """Open the picker and return the chosen paths."""
    cmd, reason = picker_command(kind, prompt, name)
    if cmd is None:
        return {"path": None, "paths": [], "reason": reason}
    try:
        # A picker nobody answers must not hold the request open forever.
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"path": None, "paths": [], "reason": "The file picker was left open too long and gave up. Paste the path in instead."}
    if done.returncode != 0 and "-128" not in done.stderr:  # -128 is the user cancelling
        raise Failed("internal_error", "The file picker would not open. Paste the path in instead.")
    paths = [line for line in done.stdout.splitlines() if line.strip()]
    return {"path": paths[0] if paths else None, "paths": paths, "reason": ""}
