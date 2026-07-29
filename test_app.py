# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "httpx"]
# ///
"""Self-check: runs the real pipeline against fake ffmpeg/whisper-cli binaries.

    uv run --script test_app.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lwt-test-"))
os.environ["LWT_DATA_DIR"] = str(TMP / "data")

import app  # noqa: E402  (all of these must follow the env var)
import config  # noqa: E402
import jobs  # noqa: E402
import library  # noqa: E402
import tools  # noqa: E402
import watch  # noqa: E402

FAKE_FFMPEG = """#!/bin/sh
echo "fake ffmpeg running" >&2
out=""
while [ $# -gt 0 ]; do out="$1"; shift; done
printf 'RIFFfake' > "$out"
"""

FAKE_FFPROBE = """#!/bin/sh
echo 123.5
"""

# Prints segments to stdout the way whisper-cli does, progress to stderr.
# With --offset-t it emits the later half only, with absolute timestamps.
FAKE_WHISPER = """#!/bin/sh
offset=0
while [ $# -gt 0 ]; do
  case "$1" in --offset-t) offset="$2"; shift ;; esac
  shift
done
echo "whisper_print_progress_callback: progress =  50%" >&2
if [ "$offset" = "0" ]; then
  echo "[00:00:00.000 --> 00:00:02.000]   shalom olam"
  echo "[00:00:02.000 --> 00:00:04.000]   ma nishma"
  [ -n "$HALF" ] && exit 137  # as if the process were killed mid-run
fi
[ -n "$SLOW" ] && sleep 30
echo "[00:00:04.000 --> 00:01:06.500]   od segment"
echo "whisper_print_progress_callback: progress = 100%" >&2
exit ${FAIL_CODE:-0}
"""


def write_fakes() -> dict:
    bin_dir = TMP / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, body in (("ffmpeg", FAKE_FFMPEG), ("ffprobe", FAKE_FFPROBE), ("whisper-cli", FAKE_WHISPER)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
        paths[name] = str(p)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # settings keys are underscored: whisper-cli -> whisper_cli_path
    config.SETTINGS.write_text(json.dumps({f"{k.replace('-', '_')}_path": v for k, v in paths.items()}))
    return paths


def fixtures() -> tuple[str, str, Path]:
    src = TMP / "my recording שלום.mp3"  # spaces + non-ASCII on purpose
    src.write_bytes(b"not really audio")
    model = TMP / "ggml-tiny.bin"
    model.write_bytes(b"not really a model")
    out = TMP / "out"
    out.mkdir(exist_ok=True)
    return str(src), str(model), out


def check(label: str, condition: bool, extra: str = "") -> None:
    assert condition, f"FAIL: {label} {extra}"
    print(f"  ok  {label}")


async def main() -> None:
    write_fakes()
    src, model, out = fixtures()

    print("happy path")
    job = jobs.make_job(src, model, str(out), "meeting-transcript")
    await jobs.run_job(job)
    check("status completed", job["status"] == "completed", job.get("error") or "")
    check("txt written", (out / "meeting-transcript.txt").exists())
    check("srt written", (out / "meeting-transcript.srt").exists())
    check("srt timestamps intact", "00:00:00,000 --> 00:00:02,000" in (out / "meeting-transcript.srt").read_text())
    check("progress parsed", job["percent"] == 100.0, str(job["percent"]))
    check("preview loaded", job["preview"].splitlines()[0] == "shalom olam", repr(job["preview"]))
    check("all segments in txt", (out / "meeting-transcript.txt").read_text().splitlines() ==
          ["shalom olam", "ma nishma", "od segment"])
    check("srt numbered from 1", (out / "meeting-transcript.srt").read_text().startswith("1\n"))
    check("srt hour rollover correct", "00:01:06,500" in (out / "meeting-transcript.srt").read_text())
    check("transcript kept out of the log", not any("shalom" in line for line in job["log"]))
    check("work dir cleaned", not (config.WORK_DIR / job["id"]).exists())
    check("source untouched", Path(src).read_bytes() == b"not really audio")
    check("history recorded", len(jobs.history()) == 1)

    print("collision detection")
    body = app.StartIn(source=src, model=model, out_dir=str(out), basename="meeting-transcript")
    check("existing files reported", len(app.collisions(body)["existing"]) == 2)
    check("fresh name is clear", not app.collisions(body.model_copy(update={"basename": "other"}))["existing"])

    print("whisper failure")
    os.environ["FAIL_CODE"] = "3"
    job = jobs.make_job(src, model, str(out), "fails")
    await jobs.run_job(job)
    del os.environ["FAIL_CODE"]
    check("status failed", job["status"] == "failed")
    check("error code", job["error"]["code"] == "whisper_failed", job["error"]["message"])
    check("no partial output left behind", not (out / "fails.txt").exists())

    print("cancellation")
    os.environ["SLOW"] = "1"
    job = jobs.make_job(src, model, str(out), "cancelled")
    jobs.JOB = job
    task = asyncio.create_task(jobs.run_job(job))
    while tools.PROC is None or job["stage"] != "transcribing":
        await asyncio.sleep(0.05)
    child = tools.PROC.pid
    app.cancel()
    await asyncio.wait_for(task, 10)
    del os.environ["SLOW"]
    check("status cancelled", job["status"] == "cancelled")
    check("no output written", not (out / "cancelled.txt").exists())
    check("child process gone", not process_alive(child))

    if sys.platform == "darwin":
        print("file picker")

        class FakeRun:
            def __init__(self, code, out="", err=""):
                self.returncode, self.stdout, self.stderr = code, out, err

        real_run = tools.subprocess.run
        try:
            tools.subprocess.run = lambda *a, **k: FakeRun(0, "/tmp/picked file.mp3\n")
            check("chosen path returned", app.pick()["path"] == "/tmp/picked file.mp3")
            tools.subprocess.run = lambda *a, **k: FakeRun(1, "", "execution error: User canceled. (-128)")
            check("cancel is not an error", app.pick()["path"] is None)
            tools.subprocess.run = lambda *a, **k: FakeRun(1, "", "osascript: no such thing")
            try:
                app.pick()
                raise AssertionError("FAIL: a broken picker was reported as success")
            except app.HTTPException as exc:
                check("broken picker surfaced", exc.status_code == 500)
        finally:
            tools.subprocess.run = real_run

    print("queue")
    first = jobs.make_job(src, model, str(out), "q-one")
    second = jobs.make_job(src, model, str(out), "q-two")
    jobs.enqueue(first)
    jobs.enqueue(second)
    check("both waiting", len(jobs.QUEUE) == 2, str(len(jobs.QUEUE)))
    await jobs.PUMP
    check("first ran", first["status"] == "completed" and (out / "q-one.txt").exists())
    check("second ran", second["status"] == "completed" and (out / "q-two.txt").exists())
    check("ran in order", first["started_at"] <= second["started_at"])
    check("queue drained", jobs.QUEUE == [])
    check("both in history", len([h for h in jobs.history() if "/q-" in str(h["outputs"])]) == 2)

    waiting = jobs.make_job(src, model, str(out), "never-runs")
    jobs.QUEUE.append(waiting)  # appended directly: enqueue would start it
    check("removed from the queue", jobs.dequeue(waiting["id"]) and jobs.QUEUE == [])
    check("removing it twice reports nothing to remove", jobs.dequeue(waiting["id"]) is False)
    try:
        app.dequeue(waiting["id"])  # the route turns that into a 404
        raise AssertionError("FAIL: removing a job twice was not rejected")
    except app.HTTPException as exc:
        check("route answers 404", exc.status_code == 404)

    print("finding tools without a useful PATH")
    real_path = os.environ["PATH"]
    real_dirs = tools.BIN_DIRS
    try:
        os.environ["PATH"] = "/usr/bin:/bin"  # what launchd hands us
        tools.BIN_DIRS = (str(TMP / "bin"),)    # stand in for /opt/homebrew/bin
        config.SETTINGS.unlink()                 # no overrides to fall back on
        check("found off PATH", tools.locate("ffmpeg") == str(TMP / "bin" / "ffmpeg"))
        check("environment agrees", tools.environment()["whisper-cli"]["ok"])
        config.SETTINGS.write_text(json.dumps({"whisper_cli_path": "/somewhere/whisper-cli"}))
        check("hyphenated override honoured", tools.locate("whisper-cli") == "/somewhere/whisper-cli")
    finally:
        os.environ["PATH"] = real_path
        tools.BIN_DIRS = real_dirs
        write_fakes()

    print("queue survives a restart")
    pending = jobs.make_job(src, model, str(out), "after-restart")
    jobs.QUEUE.append(pending)  # appended directly so the pump leaves it alone
    jobs.save(pending)
    pending["status"] = "queued"
    jobs.save(pending)
    jobs.QUEUE.clear()
    jobs.restore_queue()  # what the lifespan hook does on boot
    check("backlog picked back up", [j["id"] for j in jobs.QUEUE] == [pending["id"]],
          str([j["id"] for j in jobs.QUEUE]))
    await jobs.PUMP
    check("restored job ran", (out / "after-restart.txt").exists())

    print("a removed job stays removed")
    dropped = jobs.make_job(src, model, str(out), "dropped")
    dropped["status"] = "queued"
    jobs.save(dropped)
    jobs.QUEUE.append(dropped)
    jobs.dequeue(dropped["id"])
    jobs.QUEUE.clear()
    jobs.restore_queue()
    check("not resurrected by a restart", jobs.QUEUE == [], str(jobs.QUEUE))

    print("resume after an interrupted run")
    os.environ["HALF"] = "1"  # whisper stops after two segments, as if killed
    job = jobs.make_job(src, model, str(out), "resumed")
    await jobs.run_job(job)
    del os.environ["HALF"]
    work = config.WORK_DIR / job["id"]
    check("partial work kept", (work / "segments.txt").exists() and (work / "audio.wav").exists())
    check("checkpoint written", (work / "job.json").exists())
    offered = [r for r in jobs.resumable() if r["id"] == job["id"]]
    check("offered for resume", len(offered) == 1)
    check("reports how far it got", offered[0]["reached_ms"] == 4000, str(offered[0]))

    resumed = jobs.load_job(job["id"])
    await jobs.run_job(resumed)
    check("resumed run completed", resumed["status"] == "completed", resumed.get("error") or "")
    check("conversion was skipped", any("reusing" in line for line in resumed["log"]))
    check("resumed from the right point", any("--offset-t" in line and "4000" in line
                                              for line in resumed["log"]))
    final = (out / "resumed.txt").read_text().splitlines()
    check("both halves present exactly once", final == ["shalom olam", "ma nishma", "od segment"], str(final))
    srt = (out / "resumed.srt").read_text()
    check("resumed srt renumbered 1..3", [b.split("\n")[0] for b in srt.strip().split("\n\n")] == ["1", "2", "3"])
    check("resumed srt keeps absolute times", "00:00:04,000 --> 00:01:06,500" in srt)
    check("no longer offered", not [r for r in jobs.resumable() if r["id"] == job["id"]])

    print("work dir sweep")
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    stale, fresh = config.WORK_DIR / "stale", config.WORK_DIR / "fresh"
    stale.mkdir(exist_ok=True)
    fresh.mkdir(exist_ok=True)
    os.utime(stale, (0, 0))
    jobs.sweep_work_dirs()
    check("stale scratch removed", not stale.exists())
    check("live scratch kept", fresh.exists())

    print("path validation")
    for bad in ("relative/path.mp3", str(TMP / "nope.mp3")):
        try:
            app.resolve_file(bad, "The media file", "invalid_input_path")
            raise AssertionError(f"FAIL: accepted {bad}")
        except app.HTTPException:
            pass
    check("bad paths rejected", True)
    check("basename traversal stripped", Path("../../etc/passwd").name == "passwd")

    print("\nall checks passed")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        import shutil

        shutil.rmtree(TMP, ignore_errors=True)
