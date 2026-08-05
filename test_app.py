# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "httpx"]
# ///
"""Self-check: runs the real pipeline against fake ffmpeg/whisper-cli binaries.

    uv run --script test_app.py
"""

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lwt-test-"))
os.environ["RESCRIBE_DATA_DIR"] = str(TMP / "data")

import app  # noqa: E402  (all of these must follow the env var)
import config  # noqa: E402
import jobs  # noqa: E402
import library  # noqa: E402
import devices  # noqa: E402
import levels  # noqa: E402
import mixing  # noqa: E402
import diagnostics  # noqa: E402
import record  # noqa: E402
import retention  # noqa: E402
import saving  # noqa: E402
import selfcheck  # noqa: E402
import syshelper  # noqa: E402
import tools  # noqa: E402
import transcribe  # noqa: E402
import watch  # noqa: E402

# Writes something big enough to look like real captured audio to the recorder,
# which refuses to save a file that is only a header. With -list_devices or
# -sources it prints a device list instead and exits non-zero, as ffmpeg does.
FAKE_FFMPEG = """#!/bin/sh
# Reports loudness the way ebur128 does, so a meter driven by it can be tested.
[ -n "$REPORT_LEVEL" ] && echo "[Parsed_ametadata_1 @ 0x1] lavfi.r128.M=-23.400" >&2
echo "fake ffmpeg running" >&2
listing=""
out=""
while [ $# -gt 0 ]; do
  case "$1" in -list_devices|-sources) listing=1 ;; esac
  out="$1"
  shift
done
if [ -n "$listing" ]; then
  if [ -n "$FAKE_NO_DEVICES" ]; then exit 1; fi
  echo "[AVFoundation indev @ 0x7f9] AVFoundation video devices:" >&2
  echo "[AVFoundation indev @ 0x7f9] [0] FaceTime HD Camera" >&2
  echo "[AVFoundation indev @ 0x7f9] AVFoundation audio devices:" >&2
  echo "[AVFoundation indev @ 0x7f9] [0] MacBook Pro Microphone" >&2
  echo "[AVFoundation indev @ 0x7f9] [1] BlackHole 2ch" >&2
  echo "Auto-detected sources for pulse:" >&2
  echo "  alsa_input.pci-0000_00_1f.3.analog-stereo [Built-in Audio Analog Stereo]" >&2
  echo "  alsa_output.pci-0000_00_1f.3.analog-stereo.monitor [Monitor of Built-in Audio]" >&2
  exit 1
fi
[ -n "$RECORD_SILENCE" ] && exit 1
awk 'BEGIN{ for (i = 0; i < 8192; i++) printf "A" }' > "$out"
"""

# Answers two questions, because the app asks two: how long a file is, and how many
# channels it has. A stub that replied with a duration to both would have every
# file read as unreadable, which quietly means one track and no speaker labels —
# so it says one channel, and the checks that care about two use a real ffprobe.
FAKE_FFPROBE = """#!/bin/sh
for arg in "$@"; do
  case "$arg" in *stream=channels*) echo 1; exit 0 ;; esac
done
echo 123.5
"""

# Prints segments to stdout the way whisper-cli does, progress to stderr.
# With --offset-t it emits the later half only, with absolute timestamps.
FAKE_WHISPER = """#!/bin/sh
# Each track says something different, because two channels of a real recording do.
# When both said the same thing the echo suppression could not tell a conversation
# from a microphone hearing the speakers, and quietly removed a whole speaker.
offset=0
who=""
while [ $# -gt 0 ]; do
  case "$1" in
    --offset-t) offset="$2"; shift ;;
    *audio-1.wav) who="gam ani " ;;
  esac
  shift
done
echo "whisper_print_progress_callback: progress =  50%" >&2
if [ "$offset" = "0" ]; then
  echo "[00:00:00.000 --> 00:00:02.000]   ${who}shalom olam"
  echo "[00:00:02.000 --> 00:00:04.000]   ${who}ma nishma"
  [ -n "$HALF" ] && exit 137  # as if the process were killed mid-run
fi
[ -n "$SLOW" ] && sleep 30
echo "[00:00:04.000 --> 00:01:06.500]   ${who}od segment"
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
    # The system-audio helper is left out of the fake world on purpose. Pointing at
    # a source that is not there is what stops these checks from invoking a real
    # Swift compiler, and from reporting whichever permissions this particular
    # machine happens to have granted. The tests that want it patch it in.
    syshelper.HELPER_SOURCE = TMP / "no-syscapture.swift"
    syshelper.HELPER_BIN = TMP / "data" / "no-syscapture"
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

    print("a slow write does not stop the app")
    # A move into ~/Documents blocks in the kernel until macOS gets an answer from a
    # consent dialog, which took the whole backend down with it: the poll stopped
    # answering and the interface went dead mid-transcription. Standing in for that
    # dialog with a blocking sleep, and counting whether anything else on the event
    # loop still got a turn while it lasted.
    real_write = jobs.write_outputs
    jobs.write_outputs = lambda *a, **k: (time.sleep(0.4), real_write(*a, **k))[1]
    turns = 0

    async def still_answering() -> None:
        nonlocal turns
        while True:
            turns += 1
            await asyncio.sleep(0.02)

    poll = asyncio.create_task(still_answering())
    stalled = jobs.make_job(src, model, str(out), "stalled")
    await jobs.run_job(stalled)
    poll.cancel()
    jobs.write_outputs = real_write
    check("the loop kept running while the write waited", turns > 5, f"{turns} turns")
    check("and the write still happened", (out / "stalled.txt").exists())

    print("the wait, and what it is honest about")
    # An estimate with no evidence behind it is worse than none, because it will be
    # believed. So: nothing at all until either this run has done enough to speak
    # for itself, or the same model has been through this machine before.
    fresh = {"model": model, "duration": 600.0, "percent": 0.0, "started_at": 1000.0}
    check("nothing is claimed with nothing to claim it from",
          jobs.estimate_remaining(fresh, [], now=1010.0) is None)
    ran_before = [{"status": "completed", "model": model, "duration": 600.0, "work_seconds": 60.0},
                  {"status": "completed", "model": model, "duration": 300.0, "work_seconds": 20.0},
                  {"status": "completed", "model": "other", "duration": 60.0, "work_seconds": 600.0}]
    check("a model this machine has run before is measured, not guessed",
          jobs.past_speed(model, ran_before) == 10.0, str(jobs.past_speed(model, ran_before)))
    check("and another model's runs are not borrowed", jobs.past_speed("never-run", ran_before) is None)
    # 600s of audio at 10x is 60s of work, 10 of which have gone.
    check("so ten minutes of audio is about fifty seconds more",
          jobs.estimate_remaining(fresh, ran_before, now=1010.0) == 50.0,
          str(jobs.estimate_remaining(fresh, ran_before, now=1010.0)))
    # Once it is properly under way its own pace is better evidence than any history.
    underway = {**fresh, "percent": 25.0}
    check("a run far enough along speaks for itself",
          jobs.estimate_remaining(underway, ran_before, now=1020.0) == 60.0,
          str(jobs.estimate_remaining(underway, ran_before, now=1020.0)))
    check("and never counts backwards",
          jobs.estimate_remaining({**fresh, "percent": 100.0}, [], now=2000.0) == 0.0)

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
    check("the queue itself reports nothing removed", jobs.dequeue(waiting["id"]) is False)
    # Already gone is done, not an error: the button said remove and the job is not
    # there. Refusing made the second click do nothing and say nothing.
    check("but the route treats already gone as done",
          app.dequeue(waiting["id"]) == {"ok": True, "was": "gone"})
    running = jobs.make_job(src, model, str(out), "in-flight")
    running["status"] = "running"
    was_job, jobs.JOB = jobs.JOB, running
    try:
        # The one the button used to refuse outright, which is what made pressing it
        # look broken: a job that started between the page drawing and the click.
        check("and removes the running job by cancelling it",
              app.dequeue(running["id"]) == {"ok": True, "was": "running"})
        check("which is what cancelling looks like", running["status"] == "cancelling")
    finally:
        jobs.JOB = was_job

    print("finding tools without a useful PATH")
    real_path = os.environ["PATH"]
    real_dirs = tools.BIN_DIRS
    try:
        # An empty directory rather than the literal "/usr/bin:/bin" launchd hands
        # us. The point is a PATH with none of our tools on it, and on Linux
        # /usr/bin is exactly where a real ffmpeg lives — so on any machine that
        # had one, this found it there and never tested the fallback at all.
        empty = TMP / "no-tools-here"
        empty.mkdir(exist_ok=True)
        os.environ["PATH"] = str(empty)
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

    # Settings taken off the screen must not be settings taken away. The page sends
    # only the keys it still has fields for, and the server merges what it is given,
    # so a value with no control left keeps standing. A field removed from the page
    # but left in the save list would send a blank and wipe it — which is the one
    # way this change could have destroyed something.
    # §8 step 5: every failure the app knows about is a sentence about what happened
    # and what to do. This pins the standard rather than the wording — "Bad run id."
    # and "Not a folder: /x" pass no reading of it — so the next fragment fails here
    # rather than reaching somebody mid-meeting.
    #
    # Parsed rather than matched. The first version of this check used a regular
    # expression, and every message written across two source lines came back cut in
    # half and was reported as a fragment: eight of its fourteen findings were its
    # own doing. The syntax tree joins those the way Python does.
    # What the model heard, as against what it was told. A Hebrew recording
    # transcribed under a default of "en" said English in "how this was made", which
    # is the app repeating the instruction back rather than reporting the result —
    # and whisper does not fail at a wrong language, it invents, so this is the one
    # line that explains a transcript full of nonsense.
    print("the language it heard, not the one it was given")
    heard = tools.DETECTED.search("whisper_full_with_state: auto-detected language: he (p = 0.976300)")
    check("the detection line is read", heard is not None and heard.group(1) == "he", str(heard))
    check("and so is how sure it was", heard and round(float(heard.group(2)), 3) == 0.976)
    check("a line that is not a detection is left alone",
          tools.DETECTED.search("whisper_print_progress_callback: progress = 50%") is None)
    told = {"log": deque(maxlen=8)}
    for line in ["whisper_full_with_state: auto-detected language: he (p = 0.98)",
                 "whisper_full_with_state: auto-detected language: en (p = 0.31)"]:
        found = tools.DETECTED.search(line)
        told.setdefault("detected_language", found.group(1))
    check("the first track speaks for the recording", told["detected_language"] == "he",
          told["detected_language"])

    print("every error is a sentence")

    def said(node: ast.AST) -> str | None:
        """The message a Failed or HTTPException carries, placeholders and all."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(part.value if isinstance(part, ast.Constant) else "something"
                           for part in node.values)
        return None

    def messages(tree: ast.AST) -> list[str]:
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "Failed" and len(node.args) > 1:
                text = said(node.args[1])
                if text:
                    out.append(text)
            if node.func.id == "HTTPException":
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        for key, value in zip(arg.keys, arg.values):
                            if getattr(key, "value", "") == "message" and said(value):
                                out.append(said(value))
        return out

    fragments = []
    for name in ("app.py", "record.py", "jobs.py", "transcribe.py", "tools.py", "watch.py"):
        for text in messages(ast.parse(Path(name).read_text(encoding="utf-8"))):
            words = text.split()
            opens = text.lstrip()[:1]
            if not text.rstrip().endswith((".", "?", "!")) or len(words) < 4 or not (
                    opens.isupper() or opens in "{" or words[0].startswith(
                        ("something", "ffmpeg", "ffprobe", "whisper"))):
                fragments.append(f"{name}: {text[:64]}")
    check("no error is a fragment or a label", not fragments,
          "\n      " + "\n      ".join(fragments))

    print("what leaves the screen does not leave the settings")
    config.save_settings({"record_max_minutes": 45, "vad_model_path": "/models/silero.bin",
                          "default_language": "he"})
    config.save_settings({"default_language": "en"})       # what the page now sends
    kept = config.settings()
    check("a setting with no control left is untouched", kept["record_max_minutes"] == 45, str(kept))
    check("and so is the one whose switch was removed",
          kept["vad_model_path"] == "/models/silero.bin", str(kept))
    check("while what was sent did change", kept["default_language"] == "en")

    print("taking the transcript away")

    def picker_on(platform: str, zenity: bool, *args) -> tuple[list[str] | None, str]:
        """Ask for a picker as a given machine would answer, whatever this one is.

        Every branch then gets exercised on every runner, which is the point. This
        used to ask the machine it was running on, so the macOS branch was checked
        only on macOS, the zenity branch was checked nowhere at all, and a Linux
        runner without zenity got None and took `" ".join` down with it — killing
        the suite at this line and leaving the 600 checks after it unrun on the
        very platform they were meant to cover.
        """
        was = (tools.sys.platform, tools.shutil.which)
        tools.sys.platform = platform
        tools.shutil.which = lambda name: "/usr/bin/zenity" if zenity else None
        try:
            return tools.picker_command(*args)
        finally:
            tools.sys.platform, tools.shutil.which = was

    # The save panel is shared with the settings backup now, so it has to be told
    # what it is saving. Names come from files somebody else chose the name of, and
    # they land inside an AppleScript string literal.
    cmd, _ = picker_on("darwin", False, "save", "Save the transcript as", 'a "quoted" name.txt')
    script = " ".join(cmd)
    check("the save panel is told what it is saving", "Save the transcript as" in script, script)
    check("and a quote in a file name cannot break out of the script",
          script.count('"') % 2 == 0 and 'quoted' in script, script)
    check("the settings backup still gets its own default",
          tools.BACKUP_NAME in " ".join(picker_on("darwin", False, "save")[0]))

    zen, _ = picker_on("linux", True, "save", "", "notes.txt")
    check("zenity is asked to save, under the name it was given",
          "--save" in zen and "--filename=notes.txt" in zen, str(zen))
    none, why = picker_on("linux", False, "save")
    check("a machine with no picker offers none", none is None)
    check("and says so in a sentence rather than failing",
          why.endswith(".") and "paste" in why.lower(), why)

    print("library")
    entries = library.entries()
    check("lists what was transcribed", len(entries) >= 2, str(len(entries)))
    one = next(e for e in entries if e["id"] == job["id"])  # the resumed job above
    check("knows its files are there", one["has_text"] and one["has_cues"])
    detail = library.detail(one["id"])
    check("returns cues with absolute times", [c["start"] for c in detail["cues"]] == [0, 2000, 4000],
          str([c["start"] for c in detail["cues"]]))
    check("returns the text too", "shalom olam" in detail["text"])
    check("unknown id is not found", library.detail("deadbeefdead") is None)
    check("unknown id has no media", library.media_path("deadbeefdead") is None)
    check("media resolves to the source", library.media_path(one["id"]) == Path(src))
    hits = library.search("nishma")
    check("search finds a phrase across transcripts", len(hits) >= 1, str(hits))
    check("every hit carries the right timestamp", all(h["start"] == 2000 for h in hits), str(hits))
    check("search ignores one-letter noise", library.search("a") == [])

    print("serving audio for playback")
    from fastapi.testclient import TestClient

    client = TestClient(app.app)
    whole = client.get(f"/api/media/{one['id']}")
    check("streams the source", whole.status_code == 200 and whole.content == Path(src).read_bytes())
    part = client.get(f"/api/media/{one['id']}", headers={"Range": "bytes=0-3"})
    check("answers a range with 206", part.status_code == 206, str(part.status_code))
    check("sends exactly the bytes asked for", part.content == Path(src).read_bytes()[:4], str(part.content))
    check("unknown id is refused", client.get("/api/media/deadbeefdead").status_code == 404)

    print("the recording routes")
    check("state carries the recorder", "recording" in client.get("/api/state").json())
    check("and anything left unsaved", "orphan_recordings" in client.get("/api/state").json())
    check("stopping nothing answers 409", client.post("/api/record/stop").status_code == 409)
    check("an unknown recording cannot be saved",
          client.post("/api/record/keep/deadbeefdead").status_code == 400)
    # Refused by the router before the handler sees it; safe_id is the backstop
    # for anything that does get through.
    check("nor one named to escape the work dir",
          not client.post("/api/record/keep/..%2F..%2Fetc").is_success)
    try:
        app.safe_id("../../etc/passwd")
        raise AssertionError("FAIL: a traversing recording id was accepted")
    except app.HTTPException as exc:
        check("a traversing id is rejected outright", exc.status_code == 400)
    devices_route = client.get("/api/record/devices").json()
    check("devices are offered over http", len(devices_route["devices"]) >= 2, str(devices_route)[:120])

    print("serving the page")
    page = client.get("/")
    check("index is served", page.status_code == 200 and "<title>" in page.text)
    check("page must be revalidated, never served stale",
          page.headers.get("cache-control") == "no-cache", str(page.headers))
    check("but an unchanged file still costs nothing",
          client.get("/", headers={"If-None-Match": page.headers["etag"]}).status_code == 304)
    check("scripts get the same treatment",
          client.get("/app.js").headers.get("cache-control") == "no-cache")

    print("watched folders")
    watched = TMP / "watched" / "meeting one"
    watched.mkdir(parents=True, exist_ok=True)
    fresh = watched / "new.m4a"
    fresh.write_bytes(b"audio")
    old = watched / "old.m4a"
    old.write_bytes(b"audio")
    os.utime(old, (0, 0))
    done = watched / "done.m4a"
    done.write_bytes(b"audio")
    os.utime(done, (0, 0))
    (watched / f"done{config.TRANSCRIPT_SUFFIX}.txt").write_text("already")
    found, skipped = watch.candidates(TMP / "watched")
    names = [p.name for p in found]
    check("picks up a settled file", "old.m4a" in names, str(names))
    check("leaves a file still being written", "new.m4a" not in names)
    check("says why it skipped it", any("still being written" in s for s in skipped), str(skipped))
    check("leaves one that has a transcript beside it", "done.m4a" not in names)

    ran = watched / "ran-before.m4a"
    ran.write_bytes(b"audio")
    os.utime(ran, (0, 0))
    jobs.append_history({**jobs.make_job(str(ran), model, str(out), "ran-before"),
                         "status": "completed", "outputs": {}})
    found, skipped = watch.candidates(TMP / "watched")
    check("leaves one already run once", ran.name not in [p.name for p in found])
    check("and says so", any("already transcribed once" in s for s in skipped), str(skipped))

    for i in range(watch.MAX_PER_SWEEP + 3):
        extra = watched / f"bulk{i}.m4a"
        extra.write_bytes(b"audio")
        os.utime(extra, (0, 0))
    video_only = TMP / "watched" / "video only"
    video_only.mkdir(parents=True, exist_ok=True)
    for name in ("audio1234.m4a", "video1234.mp4"):
        f = watched / name
        f.write_bytes(b"media")
        os.utime(f, (0, 0))
    lonely = video_only / "screen-recording.mp4"
    lonely.write_bytes(b"media")
    os.utime(lonely, (0, 0))
    found, skipped = watch.candidates(TMP / "watched")
    names = [p.name for p in found]
    check("takes the audio of a recording", "audio1234.m4a" in names, str(names))
    check("skips the video beside it", "video1234.mp4" not in names, str(names))
    check("explains that skip", any("same recording" in s for s in skipped), str(skipped))
    alone, _ = watch.candidates(video_only)  # its own folder, clear of the cap test above
    check("still takes a video with no audio beside it", [p.name for p in alone] == [lonely.name],
          str(alone))

    # The real case: the audio was transcribed long ago, so it is not a candidate
    # any more, and only the video is left standing next to it.
    done_pair = TMP / "watched" / "already done"
    done_pair.mkdir(parents=True, exist_ok=True)
    for name in ("audio999.m4a", "video999.mp4"):
        f = done_pair / name
        f.write_bytes(b"media")
        os.utime(f, (0, 0))
    jobs.append_history({**jobs.make_job(str(done_pair / "audio999.m4a"), model, str(out), "audio999"),
                         "status": "completed", "outputs": {}})
    left, why = watch.candidates(done_pair)
    check("video is skipped even when the audio is long gone from the list", left == [], str(left))
    check("and it says which file it deferred to", any("same recording" in s for s in why), str(why))

    found, skipped = watch.candidates(TMP / "watched")
    check("caps one sweep", len(found) == watch.MAX_PER_SWEEP, str(len(found)))
    check("says what it left behind", any("left for the next sweep" in s for s in skipped), str(skipped))

    print("what a run cost")
    costed = jobs.make_job(src, model, str(out), "costed")
    await jobs.run_job(costed)
    check("wall time recorded", costed["work_seconds"] is not None and costed["work_seconds"] >= 0)
    check("cpu time recorded", costed["cpu_seconds"] is not None and costed["cpu_seconds"] >= 0)
    check("it reaches history", any(h.get("work_seconds") is not None for h in jobs.history()))
    shown = library.find(costed["id"])
    check("and the library can show it", shown and shown["work_seconds"] is not None, str(shown))

    print("what can be recorded from")
    listing = tools.capture([tools.binary("ffmpeg"), "-list_devices", "true"])
    _, printed = await listing
    macos = devices._parse_avfoundation(printed)
    check("reads the audio half of the mac device list",
          [d["name"] for d in macos] == ["MacBook Pro Microphone", "BlackHole 2ch"], str(macos))
    check("and leaves the cameras out of it", not any("Camera" in d["name"] for d in macos))
    check("indices come back as ffmpeg's own", [d["id"] for d in macos] == ["0", "1"], str(macos))
    linux = devices._parse_pulse(printed)
    check("reads pulse sources too", len(linux) == 2, str(linux))
    check("using the human name, not the identifier",
          "Built-in Audio Analog Stereo" in [d["name"] for d in linux], str(linux))
    check("a loopback device is recognised", devices.is_loopback({"name": "BlackHole 2ch", "id": "1"}))
    check("so is a pulse monitor",
          devices.is_loopback({"name": "Monitor of Built-in", "id": "alsa_output.x.monitor"}))
    check("a microphone is not", not devices.is_loopback({"name": "MacBook Pro Microphone", "id": "0"}))
    offered = await record.devices()
    check("the route offers what it found", len(offered["devices"]) >= 2, str(offered)[:120])
    check("and says nothing is missing", offered["advice"] == [], str(offered["advice"]))

    os.environ["FAKE_NO_DEVICES"] = "1"
    empty = await record.devices()
    del os.environ["FAKE_NO_DEVICES"]
    check("no devices at all is called out", empty["advice"] == ["noDevices"], str(empty["advice"]))

    print("the recording command")
    rec = {"voice": "0", "computer": record.SYSTEM_AUDIO,
           "devices": ["0", record.SYSTEM_AUDIO], "max_seconds": 60,
           "wav": TMP / "m.wav", "voice_wav": TMP / "voice.wav",
           "computer_wav": TMP / "computer.wav", "sys_pcm": TMP / "computer.pcm",
           "log": []}
    commands = mixing.capture_commands(rec)
    check("the driverless source needs no ffmpeg of its own", len(commands) == 1, str(commands))
    cmd = " ".join(commands[0])
    check("the microphone is captured on its own", cmd.count("-i ") == 1, cmd)
    check("into a file of its own", "voice.wav" in cmd, cmd)
    # What ruined a recording was two live devices reconciled against each other
    # inside one ffmpeg, which filled the difference with 0.237 s of silence nearly
    # four times a second and left the quieter side in pieces. One input and
    # async=1 is a different job: fill and trim against the device's own
    # timestamps, never stretch. Banning the filter outright also banned the only
    # thing that keeps the microphone's timeline honest, so the check now says
    # which use is the dangerous one.
    check("nothing is mixed while recording", "join=" not in cmd, cmd)
    check("and the capture is never stretched to fit",
          "aresample" not in cmd or "aresample=async=1," in cmd + "," , cmd)
    check("the timeline is kept the same way on both outputs",
          cmd.count("aresample=async=1") == 2, cmd)
    check("and nothing is asked of the device",
          "-ar " not in cmd and "channel_layouts" not in cmd, cmd)
    check("it stops by itself", "-t 60" in cmd, cmd)
    check("no microphone means no ffmpeg for it",
          mixing.capture_commands({**rec, "voice": ""}) == [])
    two = mixing.capture_commands({**rec, "computer": "1"})
    check("two real devices become two captures, not two inputs", len(two) == 2, str(two))
    check("each writing its own file",
          "voice.wav" in " ".join(two[0]) and "computer.wav" in " ".join(two[1]))

    print("combining the two afterwards, from finished files")
    mix = " ".join(mixing.mix_command(rec, ["voice", "computer"]))
    check("both captures become inputs", mix.count("-i ") == 2, mix)
    check("mixed here, not by the operating system",
          "join=inputs=2:channel_layout=stereo" in mix)
    check("each side flattened to mono first", mix.count("channel_layouts=mono") == 2, mix)
    check("drift corrected once, over files rather than clocks",
          "aresample=async=1000" in mix)
    check("the computer's side is the file the helper wrote, not a device",
          "computer.pcm" in mix and "avfoundation" not in mix, mix)
    check("whose raw format has to be spelled out",
          "-f s16le" in mix and "-ar 48000" in mix, mix)
    check("and the master is what comes out", "m.wav" in mix, mix)
    one = " ".join(mixing.mix_command(rec, ["voice"]))
    check("one side needs no join", "join=" not in one and one.count("-i ") == 1, one)

    print("remembering a device by what it is, not where it sits")
    listing = [{"id": "0", "name": "WH-1000XM3"}, {"id": "1", "name": "MacBook Pro Microphone"}]
    check("a remembered name finds its device again",
          devices.resolve_saved("MacBook Pro Microphone", listing) == "1")
    moved = [{"id": "0", "name": "MacBook Pro Microphone"}, {"id": "1", "name": "WH-1000XM3"}]
    check("and still finds it after the indices move",
          devices.resolve_saved("MacBook Pro Microphone", moved) == "0")
    check("a device that is gone is not guessed at",
          devices.resolve_saved("Some Old Headset", listing) == "")
    check("an index saved by an older version still works",
          devices.resolve_saved("1", listing) == "1")
    check("but not once nothing sits at it",
          devices.resolve_saved("7", listing) == "")
    check("the driverless source is not a device to look up",
          devices.resolve_saved(record.SYSTEM_AUDIO, listing) == record.SYSTEM_AUDIO)
    check("and a choice is remembered by name",
          devices.name_for("1", listing) == "MacBook Pro Microphone")
    check("the driverless one by its own id",
          devices.name_for(record.SYSTEM_AUDIO, listing) == record.SYSTEM_AUDIO)

    print("a side that recorded nothing is named")
    check("silence is reported by side",
          levels.quiet_sides({"voice": -91.0, "computer": -4.0}) == ["voice"])
    check("a healthy recording says nothing",
          levels.quiet_sides({"voice": -14.0, "computer": -4.0}) == [])
    check("a level that could not be measured is not called silent",
          levels.quiet_sides({"voice": None, "computer": -4.0}) == [])
    check("both sides can be silent at once",
          levels.quiet_sides({"voice": -91.0, "computer": -91.0}) == ["voice", "computer"])
    check("a quiet voice is not mistaken for a dead one",
          levels.quiet_sides({"voice": -45.0}) == [])

    print("which sides actually recorded")
    (TMP / "voice.wav").write_bytes(b"x" * (levels.EMPTY_WAV + 1))
    (TMP / "computer.pcm").write_bytes(b"")
    check("a side that caught nothing is not a channel",
          levels.captured_sources(rec) == ["voice"], str(levels.captured_sources(rec)))
    (TMP / "computer.pcm").write_bytes(b"x" * (levels.EMPTY_WAV + 1))
    check("and both are when both did", levels.captured_sources(rec) == ["voice", "computer"])
    (TMP / "voice.wav").unlink()
    check("a side that never started is not one either",
          levels.captured_sources(rec) == ["computer"])
    (TMP / "computer.pcm").unlink()

    # Dropping it from the channel list is right; dropping it from the conversation
    # is not. A side asked for that produced nothing never reaches the level check,
    # so nothing measured it and nothing mentioned it — the recording just came back
    # mono and the first sign of trouble was a transcript with half a meeting in it.
    both = {**rec, "voice": "1", "computer": record.SYSTEM_AUDIO}
    said = levels.silent_sides(both, ["voice"], {"voice": -12.0})
    check("a side that was asked for and never arrived is still said out loud",
          said == ["computer"], str(said))
    check("and a side that arrived silent is still said once, not twice",
          levels.silent_sides(both, ["voice", "computer"],
                              {"voice": -12.0, "computer": -91.0}) == ["computer"])
    check("while a side nobody asked for is not mentioned at all",
          levels.silent_sides({**both, "computer": ""}, ["voice"], {"voice": -12.0}) == [])

    # Each side on its own. This used to add them together and stop as soon as the
    # total moved, so a working microphone answered for a computer channel that was
    # producing nothing at all.
    #
    # A real loopback device on the computer's side, because it is the one the file
    # and the meter can honestly answer for. The tap is a different question and is
    # asked below.
    both = {**rec, "voice": "1", "computer": "2", "status": "recording"}
    (TMP / "voice.wav").write_bytes(b"x" * (levels.EMPTY_WAV + 1))
    (TMP / "computer.wav").write_bytes(b"")
    missing = await record._until_audio_arrives(both, timeout=0.3)
    check("one side arriving does not answer for the other", missing == ["computer"], str(missing))
    (TMP / "computer.wav").write_bytes(b"x" * (levels.EMPTY_WAV + 1))
    check("and nothing is reported once both are arriving",
          await record._until_audio_arrives(both, timeout=0.3) == [])

    # The meter answers before the file does. ffmpeg buffers its output, so a
    # microphone working perfectly leaves a WAV at zero bytes for tens of seconds —
    # and a check that only watched the file refused a recording that was working,
    # with the meter reading -43 dB at the time.
    (TMP / "voice.wav").write_bytes(b"")
    metered = {**both, "live": {"voice": -43.9}}
    check("a side with a meter reading is arriving, whatever the file says",
          await record._until_audio_arrives(metered, timeout=0.3) == [], "refused a working capture")
    check("and a side with neither is still missing",
          await record._until_audio_arrives({**metered, "live": {}}, timeout=0.3) == ["voice"])

    # The tap cannot be asked this question by looking at its output at all. An
    # output device playing nothing delivers no callbacks whatever — measured, 0
    # bytes with the machine quiet against 285,696 with a sound playing — so an
    # empty file means the room was quiet. Reading it as broken told somebody their
    # computer's audio was not being captured while it worked perfectly and simply
    # had nothing to capture.
    tapping = {**rec, "voice": "1", "computer": record.SYSTEM_AUDIO,
               "status": "recording", "live": {"voice": -30.0}}
    (TMP / "computer.pcm").write_bytes(b"")
    check("a quiet machine is not a broken tap",
          await record._until_audio_arrives(tapping, timeout=0.3) == [], "blamed a quiet machine")
    check("but a helper that has exited is a broken tap",
          await record._until_audio_arrives({**tapping, "helper_code": 3}, timeout=0.3) == ["computer"])
    # Digital zero is not proof of anything on its own. When a sound stops the output
    # device keeps running for a moment and hands the tap exactly that, so the tail
    # of every piece of audio would otherwise be reported as a refusal — measured
    # doing it, at the end of the check's own tone, on a machine where both sides
    # were working. It means something only where something is known to be playing,
    # which is the check and nowhere else.
    hearing_zeros = {**tapping, "live": {"voice": -30.0, "computer": -120.0}}
    check("the tail of a sound is not a refusal",
          await record._until_audio_arrives(hearing_zeros, timeout=0.3) == [],
          "warned about a working tap")
    # The helper's meter line, which is also what gives that side a level bar at last.
    # It names the side now that one process captures both of them.
    heard = syshelper.HELPER_LEVEL.search("syscapture: computer level -23.4 frames 48000")
    check("the helper's own meter is read",
          heard is not None and heard.group(1) == "computer"
          and float(heard.group(2)) == -23.4)
    for leftover in ("voice.wav", "computer.wav", "computer.pcm"):
        (TMP / leftover).unlink(missing_ok=True)

    print("the computer's audio without a driver")
    code, message = saving._why_nothing_arrived({**rec, "helper_code": syshelper.HELPER_DENIED})
    check("a refused permission is named, not guessed at",
          code == "insufficient_permissions" and "System Audio Recording Only" in message, message)
    # The list it names has to be the list macOS actually puts this app in. It used
    # to say Screen Recording, which was true of ScreenCaptureKit and is now the
    # wrong pane entirely — being sent to the wrong pane is worse than being sent
    # nowhere, because the row is not there and it reads as a lie.
    check("and not the screen list it used to be in", "Screen" not in message, message)
    check("and is not blamed on two capture sessions", "Aggregate" not in message, message)

    # Every other check that starts a recording names two real device indexes, so the
    # branch that reaches for the system-audio helper had never once been run. A call
    # left with an argument the function no longer takes sat in it through a green
    # suite, a build and an install, until the app answered a click with Internal
    # Server Error. Here the helper cannot be built — the fake world has no source —
    # so the honest refusal is what this asks for, and anything raised on the way
    # there fails instead of passing.
    try:
        reached = (await record.start("", record.SYSTEM_AUDIO)) and "no error at all"
    except config.Failed as exc:
        reached = exc.code
    except Exception as exc:  # noqa: BLE001 — a TypeError here is the bug being caught
        reached = f"{type(exc).__name__}: {exc}"
    check("asking for the computer's audio gets through to the helper",
          reached == "dependency_not_found", reached)

    async def with_helper(granted: bool) -> dict:
        """devices() as it looks on a machine where the helper exists."""
        async def fake() -> dict:
            return {"helper": Path("/x/syscapture")}
        was, syshelper.system_audio = syshelper.system_audio, fake
        try:
            return await record.devices()
        finally:
            syshelper.system_audio = was

    got = await with_helper(False)
    entry = next((d for d in got["devices"] if d["id"] == record.SYSTEM_AUDIO), None)
    check("system audio is offered as a source", entry is not None, str(got["devices"]))
    check("as a loopback, so it is the computer's side by default", bool(entry and entry["loopback"]))
    check("nobody is told to install a driver", "needLoopback" not in got["advice"], str(got["advice"]))
    # Nothing is asked for in advance any more. A process tap cannot be asked about
    # without being created, so macOS is asked at the moment of use like any other
    # application asks, and the first screen stays quiet.
    check("and nothing is asked for before anything has happened",
          got["advice"] == [], str(got["advice"]))

    # The order is the bug that broke every two-source recording. Creating the
    # aggregate device that carries a Core Audio process tap reconfigures the audio
    # HAL, and a capture opened afterwards never delivers one sample — measured both
    # ways round outside the app: microphone first and it keeps running, tap first
    # and it yields zero frames for as long as you wait.
    #
    # That ordering now lives inside the helper, which starts the microphone before
    # it builds the tap's aggregate device in one process. So what is checked here
    # has changed: not that ffmpeg was up first, but that ffmpeg is not given the
    # microphone at all when the helper can take it — because ffmpeg's avfoundation
    # input was handing over 86% of the samples the device produced, and two
    # captures started in sequence were 2.84 seconds apart.
    print("the microphone and the tap are one process")
    both_at_once = {"voice": "BuiltInMic", "computer": record.SYSTEM_AUDIO,
                    "helper": Path("/x/syscapture"), "max_seconds": 60,
                    "voice_wav": TMP / "v.wav", "computer_wav": TMP / "c.wav",
                    "sys_pcm": TMP / "c.pcm", "voice_pcm": TMP / "v.pcm",
                    "devices": ["BuiltInMic", record.SYSTEM_AUDIO], "log": []}
    check("ffmpeg is not asked for the microphone",
          mixing.capture_commands(both_at_once) == [],
          str(mixing.capture_commands(both_at_once)))
    check("and the helper is the one that takes it",
          mixing.helper_takes_the_microphone(both_at_once))
    # A loopback driver picked as the computer's side is an input device like any
    # other, so the helper takes that too. It was ffmpeg's job until it was
    # measured losing an eighth of its samples the same way the microphone did.
    loopback = {**both_at_once, "computer": "MSLoopbackDriverDevice_UID"}
    check("a loopback device on the computer's side is the helper's too",
          mixing.capture_commands(loopback) == [], str(mixing.capture_commands(loopback)))
    check("and it is named as such", mixing.helper_takes(loopback, "computer"))
    check("while the tap is not an input device to be opened",
          not mixing.helper_takes(both_at_once, "computer"))
    # Without a helper — anything that is not macOS — nothing changes.
    check("and with no helper the microphone goes back to ffmpeg",
          len(mixing.capture_commands({**both_at_once, "helper": None,
                                       "computer": "3"})) == 2)

    seen = {}
    real_helper, real_sysaudio = record._start_helper, syshelper.system_audio

    async def watching(rec: dict) -> bool:
        seen["captures_up"] = len(record.PROCS)
        return False  # far enough: what is being checked has already happened

    async def pretend() -> dict:
        return {"helper": Path("/x/syscapture")}

    record._start_helper, syshelper.system_audio = watching, pretend
    config.save_settings({"recording_folder": str(TMP / "order"), "default_model_path": model})
    try:
        # No microphone here: with one, the helper takes it and there is no ffmpeg
        # to be running first. This is the case that is left — a real device on the
        # computer's side, where the ordering rule still has something to say.
        await record.start("", record.SYSTEM_AUDIO)
        await record.TASK
    except Exception:  # noqa: BLE001 — the fake helper refuses on purpose
        pass
    finally:
        record._start_helper, syshelper.system_audio = real_helper, real_sysaudio
        # Put the module back as it was found. Refusing at the tap leaves the
        # recording half-built, and everything after this answered "a recording is
        # already running" — which is the check breaking the suite, not the code.
        record.RECORDING, record.TASK = None, None
        record.PROCS.clear()
        for stray in config.WORK_DIR.glob(f"{config.RECORDING_PREFIX}*"):
            shutil.rmtree(stray, ignore_errors=True)
    check("the tap is still asked for last", "captures_up" in seen, str(seen))

    # A check that costs six seconds on a quiet afternoon rather than the first ten
    # minutes of a meeting. It plays a tone of its own, which is the only way to tell
    # a refused tap from a quiet machine: both deliver nothing, so the machine is
    # made not quiet and the silence stops being ambiguous.
    print("checking it works before it matters")
    both_sides = ["voice", "computer"]
    good = selfcheck.check_verdict(both_sides, {"voice": -24.0, "computer": -18.0})
    check("both heard is both working", all(r["heard"] for r in good.values()), str(good))
    refused = selfcheck.check_verdict(both_sides, {"voice": -24.0, "computer": -120.0})
    check("digital silence while our own tone played is a refusal",
          refused["computer"]["why"] == "refused", str(refused["computer"]))
    absent = selfcheck.check_verdict(both_sides, {"computer": -18.0})
    check("a microphone that sent nothing at all is named as such",
          absent["voice"]["why"] == "nothing", str(absent["voice"]))
    # Two faces of the same refusal, and the tone tells them apart. A process with no
    # audio grant at all gets no callbacks rather than silent ones, so "nothing
    # arrived while our own tone was playing" is a refusal too — blaming the speakers
    # for it is what the first run of this check did.
    silent_tap = selfcheck.check_verdict(both_sides, {"voice": -24.0}, tone_played=True)
    check("nothing at all, while our tone played, is also a refusal",
          silent_tap["computer"]["why"] == "refused", str(silent_tap["computer"]))
    no_tone = selfcheck.check_verdict(both_sides, {"voice": -24.0}, tone_played=False)
    check("but if the tone never played, the speakers are what is in doubt",
          no_tone["computer"]["why"] == "output", str(no_tone["computer"]))
    murmur = selfcheck.check_verdict(["voice"], {"voice": -95.0})
    check("a microphone that heard almost nothing is quiet, not refused",
          murmur["voice"]["why"] == "quiet", str(murmur["voice"]))

    # What takes the offer off the first screen, and what puts it back. Only a check
    # where every side asked for came back counts; a passing microphone beside a
    # refused tap is exactly the state somebody most needs to be told about again.
    config.save_settings({"capture_checked": 0})
    selfcheck.remember_check(selfcheck.check_verdict(both_sides, {"voice": -24.0, "computer": -18.0}))
    check("a check where everything was heard is remembered",
          config.settings().get("capture_checked", 0) > 0)
    selfcheck.remember_check(selfcheck.check_verdict(both_sides, {"voice": -24.0, "computer": -120.0}))
    check("and one where something was not puts the offer back",
          not config.settings().get("capture_checked"))

    print("recording, then transcribing both speakers apart")
    recordings = TMP / "recordings"
    config.save_settings({"recording_folder": str(recordings), "default_model_path": model,
                          "record_label_voice": "Me", "record_label_computer": "Them",
                          "default_language": "en", "record_auto_transcribe": True})
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    live = await record.start("0", "1")
    # The fake ffmpeg exits at once, so this may already have run its whole
    # course. What matters is that starting did not report a failure.
    check("starting reported no trouble", live["status"] != "failed", str(live.get("error")))
    check("and the two sources are kept apart", live["stereo"])
    await record.TASK
    saved = record.public()
    check("saved when it finished", saved["status"] == "saved", str(saved)[:200])
    kept = Path(saved["path"])
    check("as an .m4a in the chosen folder",
          kept.suffix == ".m4a" and kept.parent == recordings, str(kept))
    check("named for when it happened", kept.stem[:2].isdigit(), kept.stem)
    check("scratch cleaned up", not list(config.WORK_DIR.glob(f"{config.RECORDING_PREFIX}*")))
    check("queued for transcription when asked to be", saved["job_id"] is not None)

    await jobs.PUMP
    # The work directory is gone once the job completes, so history is the record.
    dual = next(h for h in jobs.history() if h["id"] == saved["job_id"])
    check("completed", dual["status"] == "completed", str(dual)[:200])
    check("the job knows it has two tracks", len(dual["tracks"]) == 2, str(dual["tracks"]))
    check("one channel each", [t["channel"] for t in dual["tracks"]] == [0, 1], str(dual["tracks"]))
    transcript = (recordings / f"{kept.stem}{config.TRANSCRIPT_SUFFIX}.txt").read_text().splitlines()
    check("both speakers are in the transcript",
          any(line.startswith("Me: ") for line in transcript) and
          any(line.startswith("Them: ") for line in transcript), str(transcript))
    check("every line is owned by somebody", all(":" in line for line in transcript), str(transcript))
    check("interleaved by when it was said, not by track",
          transcript[:2] == ["Me: shalom olam", "Them: gam ani shalom olam"], str(transcript))
    check("twice the lines of one track", len(transcript) == 6, str(len(transcript)))
    srt = (recordings / f"{kept.stem}{config.TRANSCRIPT_SUFFIX}.srt").read_text()
    check("subtitles are labelled too", "Me: shalom olam" in srt)
    check("and renumbered across both", "\n6\n" in srt, srt[-120:])

    print("one channel at a time")
    channel_job = jobs.make_job(src, model, str(out), "channels")
    await transcribe.to_wav(channel_job, src, TMP / "left.wav", channel=1)
    check("a channel is pulled out by name",
          any("pan=mono|c0=c1" in line for line in channel_job["log"]), str(list(channel_job["log"])))
    plain_job = jobs.make_job(src, model, str(out), "plain")
    await transcribe.to_wav(plain_job, src, TMP / "flat.wav")
    check("and left alone when there is only one",
          not any("pan=" in line for line in plain_job["log"]))

    merged = transcribe.merge_tracks([("Me", [(0, 1000, "first"), (4000, 5000, "third")]),
                                      ("Them", [(2000, 3000, "second")])])
    check("merging sorts by time", [text for _, _, text in merged] ==
          ["Me: first", "Them: second", "Me: third"], str(merged))
    unlabelled = transcribe.merge_tracks([("", [(0, 1, "bare")])])
    check("an unlabelled track is left as it was", unlabelled == [(0, 1, "bare")], str(unlabelled))

    print("the microphone's echo of the speakers")
    echoed = [("Me", [(0, 3000, "Testing, testing, one two three"), (4000, 6000, "that was the video")]),
              ("Them", [(200, 3200, "Testing testing one, two, three.")])]
    lines = transcribe.merge_tracks(echoed)
    check("the machine's copy is the one kept",
          [l for l in lines if "Testing" in l[2]] == [(200, 3200, "Them: Testing testing one, two, three.")],
          str(lines))
    check("and what the person actually said survives",
          any("that was the video" in l[2] for l in lines), str(lines))
    apart = [("Me", [(0, 3000, "shall we start")]), ("Them", [(9000, 11000, "shall we start")])]
    check("the same words at a different time are two people agreeing",
          len(transcribe.merge_tracks(apart)) == 2, str(transcribe.merge_tracks(apart)))
    unrelated = [("Me", [(0, 3000, "I think we should ship it")]),
                 ("Them", [(500, 2500, "Testing testing one two three")])]
    check("talking over the audio keeps both",
          len(transcribe.merge_tracks(unrelated)) == 2, str(transcribe.merge_tracks(unrelated)))
    check("a single track is never thinned",
          len(transcribe.merge_tracks([("", [(0, 1, "alone")])])) == 1)

    print("the progress bar across two tracks")
    spanned = jobs.make_job(src, model, str(out), "spanned")
    spanned["percent_base"], spanned["percent_span"] = 50.0, 50.0
    await tools.stream([tools.binary("whisper-cli")], spanned, "whisper_failed")
    check("the second track fills the second half", spanned["percent"] == 100.0, str(spanned["percent"]))
    check("track names appear in the job", jobs.track_files(Path("/w"), 1, 2)[1].name == "segments-1.txt")
    check("and not when there is only one", jobs.track_files(Path("/w"), 0, 1)[1].name == "segments.txt")

    print("a meter that measures something")
    fed = {"log": [], "live": {}}
    check("loudness is read off the capture",
          levels.EBUR128_M.search("[Parsed_ametadata_1 @ 0x1] lavfi.r128.M=-23.400")
          .group(1) == "-23.400")
    check("and a line without it is not mistaken for one",
          levels.EBUR128_M.search("Guessed Channel Layout: mono") is None)
    cmd = " ".join(mixing.capture_command(
        {"max_seconds": 60, "voice": "0"}, "0", Path("/tmp/v.wav")))
    check("the capture reports how loud it is", "ebur128=metadata=1" in cmd, cmd)
    check("into an output that keeps nothing", "-f null -" in cmd, cmd)
    check("printed, since the filter alone prints nothing",
          "ametadata=print:key=lavfi.r128.M" in cmd, cmd)
    check("and the recording is still the last thing on the line",
          cmd.rstrip().endswith("/tmp/v.wav"), cmd)

    print("a disk that is filling up")
    check("plenty of room is not low", not record.disk_is_low(50_000_000_000))
    check("a few hundred megabytes is", record.disk_is_low(100_000_000))
    check("the line is where the constant says",
          record.disk_is_low(record.LOW_DISK - 1) and not record.disk_is_low(record.LOW_DISK))
    watched = {"status": "recording", "log": [], "id": "x"}
    async def stops_when_full():
        real, record.LOW_DISK = record.LOW_DISK, 10 ** 18   # every disk is full now
        try:
            await record._watch_disk(watched, poll=0.01)
        finally:
            record.LOW_DISK = real
    await stops_when_full()
    check("a recording is stopped rather than left to fill the disk",
          watched["status"] == "stopping" and watched.get("low_disk") is True, str(watched))
    check("and the log says why", any("disk" in line for line in watched["log"]), str(watched["log"]))
    quiet = {"status": "saved", "log": []}
    await record._watch_disk(quiet, poll=0.01)
    check("a recording already over is left alone", quiet["status"] == "saved")

    print("a recording that captured nothing")
    record.RECORDING = None
    os.environ["RECORD_SILENCE"] = "1"
    try:
        await record.start("0", "")
        raise AssertionError("FAIL: a silent recording was reported as working")
    except config.Failed as exc:
        check("refused rather than saved", exc.code in ("recording_failed", "insufficient_permissions"),
              exc.code)
    finally:
        del os.environ["RECORD_SILENCE"]
    check("nothing was written to the recordings folder", len(list(recordings.glob("*.m4a"))) == 1,
          str(list(recordings.glob("*.m4a"))))
    check("and no empty scratch left lying about",
          not list(config.WORK_DIR.glob(f"{config.RECORDING_PREFIX}*")),
          str(list(config.WORK_DIR.glob(f"{config.RECORDING_PREFIX}*"))))
    try:
        await record.stop()
        raise AssertionError("FAIL: stopping nothing was allowed")
    except config.Failed as exc:
        check("stopping nothing is refused", exc.code == "not_recording")

    print("audio the app died before saving")
    record.RECORDING = None
    stranded = config.WORK_DIR / f"{config.RECORDING_PREFIX}deadbeef1234"
    stranded.mkdir(parents=True, exist_ok=True)
    # What a crash actually leaves: the two captures, side by side, never combined.
    (stranded / "voice.wav").write_bytes(b"A" * 96000)      # a second of 48 kHz mono
    (stranded / "computer.pcm").write_bytes(b"B" * 96000)
    (stranded / "recording.json").write_text(json.dumps({
        "id": "deadbeef1234", "status": "recording", "devices": ["0", "1"],
        "labels": ["Me", "Them"], "folder": str(recordings), "basename": "rescued",
        "started_at": 1.0, "transcribe": False}))
    # A day old: well past the six hours that clears ordinary scratch, and inside
    # the long reprieve that captured audio gets.
    day_ago = time.time() - 86400
    os.utime(stranded, (day_ago, day_ago))
    jobs.sweep_work_dirs()
    check("the sweep leaves unsaved audio alone", (stranded / "voice.wav").exists())
    waiting = record.orphans()
    check("offered back", [r["id"] for r in waiting] == ["deadbeef1234"], str(waiting))
    check("with how long it is", waiting[0]["seconds"] == 1.0, str(waiting[0]))
    rescued = await record.keep_orphan("deadbeef1234")
    check("saving it works", rescued["status"] == "saved", str(rescued)[:160])
    check("landing where it was going", (recordings / "rescued.m4a").exists())
    check("and it is no longer offered", record.orphans() == [], str(record.orphans()))
    check("not queued when settings say not to", rescued["job_id"] is None)

    record.RECORDING = None
    config.save_settings({"recording_folder": "", "record_voice_device": "",
                          "record_computer_device": ""})

    print("source folders, looked at on demand")
    config.save_settings({"source_folders": [str(TMP / "watched")], "output_folder": ""})
    waiting_now = watch.pending()
    check("reports what is waiting", waiting_now["count"] > 0, str(waiting_now)[:120])
    check("names them", len(waiting_now["names"]) == waiting_now["count"])
    config.save_settings({"source_folders": []})
    check("nothing configured means nothing waiting", watch.pending()["count"] == 0)

    print("where transcripts go")
    elsewhere = TMP / "all-transcripts"
    elsewhere.mkdir(exist_ok=True)
    config.save_settings({"output_folder": str(elsewhere)})
    check("a chosen folder is used", watch.output_folder_for(Path(src)) == str(elsewhere))
    config.save_settings({"output_folder": ""})
    check("otherwise it sits beside the recording",
          watch.output_folder_for(Path(src)) == str(Path(src).parent))
    config.save_settings({"output_folder": "/no/such/folder"})
    check("a folder that vanished falls back rather than failing",
          watch.output_folder_for(Path(src)) == str(Path(src).parent))
    config.save_settings({"output_folder": ""})

    print("settings")
    config.save_settings({"default_language": "he", "vad_model_path": "/models/silero.bin"})
    check("keeps what was already there", config.settings().get("whisper_cli_path", "").endswith("whisper-cli"))
    check("stores a new value", config.settings()["default_language"] == "he")

    app.put_settings(app.SettingsIn(vocabulary="Grafana"))
    check("a partial save leaves other fields alone",
          config.settings()["vad_model_path"] == "/models/silero.bin" and
          config.settings()["vocabulary"] == "Grafana", str(config.settings()))
    app.put_settings(app.SettingsIn(vad_model_path=""))
    check("an empty value clears the field, so vad can be switched off",
          config.settings()["vad_model_path"] == "", str(config.settings()))
    check("and clearing one leaves the rest", config.settings()["vocabulary"] == "Grafana")
    app.put_settings(app.SettingsIn(vocabulary=""))
    models_dir = TMP / "models"
    models_dir.mkdir(exist_ok=True)
    (models_dir / "ggml-medium.bin").write_bytes(b"x" * 900)
    (models_dir / "ggml-silero-v5.1.2.bin").write_bytes(b"x" * 100)  # a VAD model, not a transcriber
    real_model_dirs = tools.MODEL_DIRS
    try:
        tools.MODEL_DIRS = (models_dir,)
        tools.find_models.cache_clear()
        found = tools.find_models()
        names = [m["name"] for m in found]
        # The catalogue's name for it, not the file's stem: what is offered to
        # somebody choosing should read like a choice, not like a filename.
        check("offers real models", "Medium" in names, str(names))
        check("and says what that one is like",
              found[0]["description"].startswith("Between"), str(found[0]))
        check("never offers the vad model as a transcriber", "silero-v5.1.2" not in names, str(names))
    finally:
        tools.MODEL_DIRS = real_model_dirs
        tools.find_models.cache_clear()

    vad_job = jobs.make_job(src, model, str(out), "vad", vad_model="/models/silero.bin")
    cmd = " ".join(transcribe.whisper_command(vad_job, Path("/tmp/a.wav")))
    check("vad flags only when a model is set", "--vad --vad-model /models/silero.bin" in cmd, cmd)
    plain = " ".join(transcribe.whisper_command(job, Path("/tmp/a.wav")))
    check("no vad flags otherwise", "--vad" not in plain)
    # VAD used to be kept off a two-speaker job, because it removes the silence and
    # then emits one segment holding everything either side of it. That was the right
    # observation and the wrong remedy: VAD's placement is accurate to 100 ms, and
    # turning it off left whisper to segment a whole track by itself — which is how a
    # word said at 13.061 s came back as `00:12 --> 00:21` in an 18-second file.
    #
    # The remedy is one segment per word, regrouped afterwards against the regions
    # VAD measured. So the invariant is no longer "no VAD here" but "never VAD
    # without the splitting", which is the combination that merges.
    two_track = jobs.make_job(src, model, str(out), "two", vad_model="/models/silero.bin",
                              tracks=[{"channel": 0, "label": "Me"},
                                      {"channel": 1, "label": "Them"}])
    both = " ".join(transcribe.whisper_command(two_track, Path("/tmp/a.wav")))
    check("two speakers get vad as well", "--vad --vad-model /models/silero.bin" in both, both)
    for name, line in (("one speaker", cmd), ("two speakers", both)):
        check(f"and {name} never gets vad without word splitting",
              "--vad" not in line or "--max-len 1 --split-on-word" in line, line)
    check("nothing is asked to split when there is no vad to repair",
          "--max-len" not in plain, plain)
    # -np would silence the region lines the timestamps are rebuilt from.
    check("and the regions are never suppressed", "-np" not in both.split(), both)

    vocab_job = jobs.make_job(src, model, str(out), "vocab", vocabulary=" Grafana, escalation  ")
    cmd = transcribe.whisper_command(vocab_job, Path("/tmp/a.wav"))
    check("vocabulary is passed as one argument, not split",
          "Grafana, escalation" in cmd and cmd[cmd.index("Grafana, escalation") - 1] == "--prompt", str(cmd))
    check("and carried past the first window", "--carry-initial-prompt" in cmd)
    blank = transcribe.whisper_command(jobs.make_job(src, model, str(out), "b", vocabulary="   "),
                                       Path("/tmp/a.wav"))
    check("whitespace is not a vocabulary", "--prompt" not in blank)

    print("saving and loading settings as a file")
    backup = TMP / "backup.json"
    config.save_settings({"vocabulary": "before-backup"})
    written = app.export_settings(app.BackupIn(path=str(backup), display={"reading_size": "1.18rem"}))
    check("written where asked", Path(written["path"]) == backup and backup.exists())
    check("it is our own kind of file", json.loads(backup.read_text())["kind"] == app.BACKUP_KIND)
    check("a missing .json is added", Path(app.export_settings(
        app.BackupIn(path=str(TMP / "noext"), display={}))["path"]).suffix == ".json")

    config.save_settings({"vocabulary": "after-backup"})
    loaded = app.import_settings(app.PathIn(path=str(backup)))
    check("settings come back", config.settings()["vocabulary"] == "before-backup")
    check("display preferences travel too", loaded["display"]["reading_size"] == "1.18rem")

    junk = TMP / "junk.json"
    junk.write_text('{"hello": 1}')
    for bad, why in ((junk, "someone else's json"), (TMP / "ggml-tiny.bin", "not json at all")):
        try:
            app.import_settings(app.PathIn(path=str(bad)))
            raise AssertionError(f"FAIL: accepted {why}")
        except app.HTTPException as exc:
            check(f"refuses {why}", exc.status_code == 400)

    # The helper captures both sides now and says which one each level belongs to.
    # The parser read group(1) as the level when that was the only thing on the
    # line, so a labelled line would have handed it a side name to float().
    both = {"log": deque(maxlen=8), "ever": set()}
    for raw in ("syscapture: voice level -31.2 frames 4800",
                "syscapture: computer level -20.7 frames 4800"):
        found = syshelper.HELPER_LEVEL.search(raw)
        assert found, raw
        both.setdefault("peak", {})[found.group(1)] = float(found.group(2))
    check("each side's level lands under its own name",
          both["peak"] == {"voice": -31.2, "computer": -20.7}, str(both["peak"]))

    # Clearing the microphone grant emptied the device listing, and the empty
    # listing was written back over the remembered choice — after which every
    # recording was the computer's side alone, silently.
    check("a device that cannot be seen keeps its name", devices.name_for("uid-1", []) == "uid-1")
    check("and nothing chosen names nothing", devices.name_for("", []) == "")

    # Picking a file that had already been transcribed refused, so the menu bar
    # opened the window and did nothing — which reads as a broken menu item. The
    # window can ask about overwriting; the menu bar has nowhere to.
    here = TMP / "already"
    here.mkdir(exist_ok=True)
    check("a name nothing uses is left alone", app.free_basename(here, "talk") == "talk")
    (here / "talk.txt").write_text("a transcript somebody may have corrected")
    check("and one that is taken steps aside", app.free_basename(here, "talk") == "talk-2")
    (here / "talk-2.srt").write_text("")
    check("as many times as it needs to", app.free_basename(here, "talk") == "talk-3")
    check("nothing already written is overwritten",
          (here / "talk.txt").read_text().startswith("a transcript"))

    print("pausing takes the time out rather than filling it")
    # A pause is not an interruption and must not be treated as one. An
    # interruption is kept as the silence it was, because the meeting carried on in
    # the room; a pause is somebody saying this time does not belong to the
    # recording, so it is closed up. The clock has to agree with the file, or the
    # window shows a length the recording does not have.
    held = {"started_at": time.time() - 60, "ended_at": None,
            "paused_at": None, "paused_total": 20.0}
    check("time spent paused is not counted", 39.5 < record.recorded_seconds(held) < 40.5,
          str(record.recorded_seconds(held)))
    still = {**held, "paused_at": time.time() - 5}
    check("nor is a pause still going on", 34.5 < record.recorded_seconds(still) < 35.5,
          str(record.recorded_seconds(still)))
    check("and it never goes backwards",
          record.recorded_seconds({**held, "paused_total": 1e9}) == 0.0)
    record.RECORDING = None
    try:
        await record.pause()
        raise AssertionError("FAIL: paused something that was not recording")
    except record.Failed as exc:
        check("pausing nothing says so in a sentence", exc.message.endswith("."), exc.message)

    # A model file is described to somebody who did not choose it, so the label has
    # to be true. large-v3-turbo matched /large/ and read as the best model on the
    # machine; it is a faster, less accurate cut of large-v3, so the label sent
    # people to a slow choice for a wrong reason.
    # What a model is called is said by the catalogue, not guessed from its file
    # name. The guess read `ggml-large-v3-turbo.bin` as the best model on the
    # machine because "large" is in it — it is a faster, less accurate cut.
    cat = json.loads(config.CATALOGUE.read_text(encoding="utf-8"))
    check("the catalogue is generated, not hand-written",
          "gen_models.py" in (Path("tools/gen_models.py").read_text()[:400]))
    check("and pinned to a revision", len(cat.get("revision", "")) >= 20, str(cat.get("revision")))
    for m in cat["models"]:
        check(f"{m['id']} carries a size and a hash to check it by",
              m["size_bytes"] > 1_000_000 and len(m["sha256"]) == 64, str(m)[:120])
        check(f"and says what it is like in a sentence",
              m["description"].endswith(".") and len(m["description"]) > 20, m["description"])
    turbo = tools.describe("/x/ggml-large-v3-turbo.bin")
    big = tools.describe("/x/ggml-large-v3.bin")
    check("turbo is not called the best model there is",
          turbo["accuracy"] < big["accuracy"] and turbo["speed"] > big["speed"], str(turbo))
    check("but it is the one offered first", turbo["rank"] < big["rank"])
    stranger = tools.describe("/x/ggml-somebody-elses.bin")
    check("a model nobody knows keeps its name and claims nothing",
          stranger["accuracy"] is None and stranger["rank"] == 999, str(stranger))
    # Largest-first meant the interface preselected whatever was biggest, so a
    # machine with Small and Large v3 defaulted to three slow gigabytes.
    order = [tools.describe(f"/x/{m['filename']}")["rank"] for m in cat["models"]]
    check("the catalogue orders by judgement, not by size", order == sorted(order), str(order))

    print("getting a model without leaving the app")
    import models as model_store
    import hashlib, http.server, threading, functools

    body = bytes(range(256)) * 4000          # 1,024,000 bytes of something checkable
    digest = hashlib.sha256(body).hexdigest()
    served = TMP / "served"
    served.mkdir(exist_ok=True)
    (served / "ggml-fake.bin").write_bytes(body)

    class Ranged(http.server.SimpleHTTPRequestHandler):
        """Honours Range, which is the whole point of the resume."""
        def log_message(self, *a): pass
        def do_GET(self):
            start = 0
            asked = self.headers.get("Range", "")
            if asked.startswith("bytes="):
                start = int(asked.removeprefix("bytes=").split("-")[0])
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            self.wfile.write(body[start:])

    server = http.server.HTTPServer(("127.0.0.1", 0), Ranged)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    where = TMP / "downloaded"
    where.mkdir(exist_ok=True)
    was_home, was_cat = model_store.HOME, model_store.catalogue
    was_dirs = tools.MODEL_DIRS
    try:
        model_store.HOME = where
        tools.MODEL_DIRS = (where,)   # so a model that lands here is found again
        entry = {"id": "fake", "filename": "ggml-fake.bin", "name": "Fake",
                 "url": f"http://127.0.0.1:{port}/ggml-fake.bin",
                 "size_bytes": len(body), "sha256": digest, "description": "For a test.",
                 "speed": 1, "accuracy": 1, "recommended": False, "rank": 0}
        model_store.catalogue = lambda: {"ggml-fake.bin": entry}

        # Half of it already on disk, as a cancelled download leaves behind.
        part = where / "ggml-fake.bin.part"
        part.write_bytes(body[:400_000])
        await model_store.download("fake")
        await model_store.TASK
        got = where / "ggml-fake.bin"
        check("the download finishes", got.is_file(), str(list(where.iterdir())))
        check("and it is the whole file, not the half plus a whole",
              got.read_bytes() == body, f"{got.stat().st_size} bytes, wanted {len(body)}")
        check("nothing half-finished is left beside it", not part.exists())

        # A file whose hash does not match is thrown away rather than used: half a
        # model makes whisper-cli fail in a way nobody could diagnose.
        got.unlink()
        model_store.catalogue = lambda: {"ggml-fake.bin": {**entry, "sha256": "0" * 64}}
        await model_store.download("fake")
        await model_store.TASK
        check("a file that is not what was promised is refused", not got.exists())
        check("and it says so rather than failing silently",
              (model_store.public() or {}).get("error", "").endswith("Try again."),
              str(model_store.public()))
        model_store.BUSY = None

        model_store.catalogue = lambda: {"ggml-fake.bin": entry}
        check("a model nobody has is offered", model_store.catalogued()[0]["have"] is False)
        await model_store.download("fake")
        await model_store.TASK
        tools.find_models.cache_clear()
        check("and once it is here it is not offered again",
              model_store.catalogued()[0]["have"] is True, str(model_store.catalogued()))
        model_store.forget("fake")
        check("deleting takes it away", not got.exists())
    finally:
        server.shutdown()
        model_store.HOME, model_store.catalogue = was_home, was_cat
        tools.MODEL_DIRS = was_dirs
        tools.find_models.cache_clear()

    print("a bad byte does not cost somebody their settings")
    # This was live: a file that would not parse came back as {}, and the next save
    # merged onto that nothing and wrote it out. One corrupt byte plus one visit to
    # settings destroyed model, folders, devices, labels and tool paths, silently.
    keep = dict(config.settings())
    try:
        good = {"default_model_path": "/models/big.bin", "record_label_voice": "Me",
                "recording_folder": "/Users/x/Recordings", "vocabulary": "Kubernetes"}
        config.save_settings(good)
        whole = config.SETTINGS.read_text()
        # A write cut off partway through, which is how the file gets broken at all.
        config.SETTINGS.write_text(whole[:len(whole) * 2 // 3])
        rescued = config.settings()
        check("what can still be read is kept", len(rescued) >= 2, str(rescued))
        check("and it is the real values, not defaults",
              rescued.get("default_model_path") == "/models/big.bin", str(rescued))
        # The moment that used to destroy everything: a save on top of a broken file.
        config.save_settings({"default_language": "he"})
        after = config.settings()
        check("saving on top of it does not wipe the rest",
              after.get("default_model_path") == "/models/big.bin", str(after))
        check("and the new value went in", after.get("default_language") == "he")
        config.SETTINGS.write_text("{ this is not json at all")
        check("something unreadable costs only itself", config.settings() == {})
        # Nothing half-written is ever left where the real file is read from.
        config.save_settings(good)
        check("the file is replaced rather than overwritten in place",
              not config.SETTINGS.with_suffix(".json.new").exists())
        check("and it parses cleanly afterwards",
              json.loads(config.SETTINGS.read_text())["vocabulary"] == "Kubernetes")
    finally:
        config.SETTINGS.write_text(json.dumps(keep, indent=2))

    print("the vad model ships with the app")
    # Without one there is no fix at all: measured, word splitting on its own
    # reproduces the same eleven-second spans. It used to be a curl command in
    # settings for the user to run by hand, which made correct timestamps optional.
    check("a model is bundled", config.BUNDLED_VAD.is_file(), str(config.BUNDLED_VAD))
    check("and it is small enough to carry", config.BUNDLED_VAD.stat().st_size < 2_000_000,
          str(config.BUNDLED_VAD.stat().st_size))
    check("it goes where the app bundle already looks",
          "mac" in config.BUNDLED_VAD.parts, str(config.BUNDLED_VAD))
    config.save_settings({"vad_model_path": ""})
    check("nothing chosen falls back to the bundled one",
          config.vad_model() == str(config.BUNDLED_VAD), config.vad_model())
    config.save_settings({"vad_model_path": "/nowhere/gone.bin"})
    check("and so does a choice that has since been deleted",
          config.vad_model() == str(config.BUNDLED_VAD), config.vad_model())
    mine = TMP / "ggml-my-vad.bin"
    mine.write_bytes(b"x")
    config.save_settings({"vad_model_path": str(mine)})
    check("a model somebody chose is still theirs", config.vad_model() == str(mine))
    config.save_settings({"vad_model_path": ""})

    print("words are put back where they were said")
    # The exact shape of the fault, as measured: an 18-second file, "one" at 1.006 s
    # and "five" at 13.061 s on one channel. VAD found both to within 100 ms; whisper
    # then handed back a single segment covering the pair, and without VAD it invented
    # `00:12 --> 00:21` — three seconds past the end of the recording.
    lines = ("whisper_vad: vad_segment_info: orig_start: 0.96, orig_end: 1.44, "
             "vad_start: 0.00, vad_end: 0.48\n"
             "whisper_vad: vad_segment_info: orig_start: 13.09, orig_end: 13.47, "
             "vad_start: 0.68, vad_end: 1.06\n")
    regions_file = TMP / "regions.txt"
    regions_file.write_text(lines)
    found = transcribe.parse_regions(regions_file)
    check("the regions whisper measured are read back", found == [(960, 1440), (13090, 13470)],
          str(found))

    # One segment per word, which is what --max-len 1 --split-on-word produces. The
    # second word's end is deliberately absurd — whisper really does run a segment
    # past the end of the audio it was given, and the region has to stop it.
    words = [(1010, 1300, "One,"), (13120, 21000, "five.")]
    said = transcribe.regroup(words, found)
    check("each word goes back into its own region", len(said) == 2, str(said))
    check("the first is where it was said", abs(said[0][0] - 1006) < 500, str(said[0]))
    check("and so is the second", abs(said[1][0] - 13061) < 500, str(said[1]))
    check("nothing ends after the recording does", said[1][1] <= 13470, str(said[1]))
    check("and nothing spans the silence between them", said[0][1] < said[1][0], str(said))

    # Two words inside one region are one sentence, not two.
    together = transcribe.regroup([(1000, 1200, "One"), (1250, 1400, "two.")], [(960, 1440)])
    check("words in one breath stay in one line", len(together) == 1
          and together[0][2] == "One two.", str(together))
    # A pause long enough to be a full stop splits, even inside a region.
    apart = transcribe.regroup([(1000, 1200, "One"), (3000, 3200, "two.")], [(900, 4000)])
    check("a long pause inside a region still splits", len(apart) == 2, str(apart))
    # With nothing measured, the words are handed back untouched rather than moved.
    check("no regions means nothing is invented",
          transcribe.regroup(words, []) == words)
    # Both found in a real 21-minute meeting rather than imagined: a word starting
    # past the end of its region left a line with no length, and clamping left one
    # line beginning before the line above it had ended.
    squashed = transcribe.regroup([(1500, 1500, "Right.")], [(1000, 1500)])
    check("no line is a single point in time", squashed[0][1] > squashed[0][0], str(squashed))
    stacked = transcribe.regroup([(1000, 5000, "First."), (2000, 3000, "Second.")],
                                 [(900, 5100), (1900, 3100)])
    check("and no line starts before the one above it ends",
          all(stacked[i][1] <= stacked[i + 1][0] for i in range(len(stacked) - 1)), str(stacked))

    print("the interface says everything in one language")
    page = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    strings = (config.WEB_DIR / "i18n.js").read_text(encoding="utf-8")

    # Anything the page translates has to be parsed before the scripts that do the
    # translating. applyTranslations() runs as they load, so markup written below
    # them keeps whatever the HTML said — which is how a Hebrew question came with
    # an English Yes and Cancel under it.
    first_script = page.index('<script src="/')
    late = [line.strip()[:60] for line in page[first_script:].splitlines()
            if "data-i18n" in line]
    check("nothing translatable is written after the scripts", late == [], str(late))

    # Every key the page asks for, in both languages. A missing one falls through
    # to English silently, which is the same bug wearing a different hat.
    wanted = set(re.findall(r'data-i18n(?:-placeholder|-title)?="([^"]+)"', page))
    for lang in ("en", "he"):
        body = strings.split(f"  {lang}: {{", 1)[1]
        have = set(re.findall(r'"([\w.]+)":', body))
        missing = sorted(k for k in wanted if k not in have)
        check(f"{lang} has every key the page uses", missing == [], str(missing[:6]))

    # And the words the backend hands the page to print. `job.was.<status>` is one
    # of these: the raw status went straight into a sentence, so a Hebrew reader
    # got "cancelled" in the middle of it.
    for status in ("cancelled", "failed", "running", "queued"):
        for lang in ("en", "he"):
            body = strings.split(f"  {lang}: {{", 1)[1]
            check(f"{lang} can say a run was {status}",
                  f'"job.was.{status}"' in body, f"job.was.{status} missing from {lang}")

    print("one line for the menu bar")
    record.RECORDING = None
    jobs.JOB = None
    check("nothing happening says so", record.glance() == "idle")
    record.RECORDING = {"status": "recording", "started_at": time.time() - 42, "ended_at": None}
    said = record.glance()
    check("a recording gives its seconds", said.split()[0] == "recording"
          and 41 <= int(said.split()[1]) <= 43, said)
    record.RECORDING = None
    jobs.JOB = {"status": "running", "percent": 63.4}
    check("a transcription gives whole percent", record.glance() == "working 63", record.glance())
    jobs.JOB = None
    # The menu bar splits this on a space and reads the pieces positionally, so a
    # line that ever grew a newline or a third word would quietly break it.
    check("and it is always one short line", "\n" not in record.glance()
          and len(record.glance().split()) <= 2, record.glance())

    print("a meter that can show a voice")
    meter_cmd = " ".join(mixing.capture_commands(rec)[0])
    check("loudness is still measured, since the checks are built on it",
          "ebur128" in meter_cmd, meter_cmd)
    check("and a peak alongside it, which is what the needle moves on",
          "astats" in meter_cmd and "Peak_level" in meter_cmd, meter_cmd)
    check("over a window this app fixes rather than the device",
          "asetnsamples=n=2400" in meter_cmd, meter_cmd)
    # The output that writes the WAV gets the timeline fix and nothing else. Metering
    # filters on the file being kept would be work done on the one path that cannot
    # afford to fall behind.
    argv = mixing.capture_commands(rec)[0]
    kept = argv[argv.index("-c:a") - 1]
    check("none of the metering reaches the file being kept",
          kept == mixing.KEEP_TIME, kept)

    live = {"log": deque(maxlen=8)}
    for line in ("[Parsed_ametadata_3 @ 0x0] lavfi.astats.Overall.Peak_level=-18.06",
                 "[Parsed_ametadata_1 @ 0x0] lavfi.r128.M=-24.5"):
        found = levels.PEAK.search(line)
        if found:
            live.setdefault("peak", {})["voice"] = float(found.group(1))
    check("a peak is read off the meter", live["peak"]["voice"] == -18.06)
    # astats calls a window of digital zero -inf, which float() will not take and
    # which would otherwise throw inside the drain loop and end the capture's log.
    quiet = levels.PEAK.search("[Parsed_ametadata_3 @ 0x0] lavfi.astats.Overall.Peak_level=-inf")
    check("and digital silence does not come back as a crash", quiet is not None, "no match")
    check("it is read as the -120 the helper already uses for it",
          levels.DIGITAL_SILENCE == -120.0)

    record.RECORDING = None
    check("with nothing recording the needles say so and carry nothing",
          record.meters() == {"recording": False, "peak": {}})

    print("a stalled capture keeps its place in time")
    # The measurement this exists for: a Mac put to sleep mid-recording came back
    # with 31 of 70 seconds missing, both captures alive throughout, and the hole
    # closed up rather than left open — which moves every timestamp after it.
    rec = {"log": deque(maxlen=8)}
    levels._heard(rec, "voice")
    rec["moved"]["voice"] -= 5.0          # five seconds in which nothing arrived
    levels._heard(rec, "voice")
    gaps = rec["gaps"]["voice"]
    check("a stall is noticed at all", len(gaps) == 1)
    check("and measured against the wall clock", 4.8 < gaps[0][1] < 5.0)
    check("and placed where the audio stopped", gaps[0][0] == 0.1)
    check("a side that has just spoken is not called stalled", levels.stalled_sides(rec) == [])
    rec["moved"]["voice"] -= levels.STALL + 1
    check("one that has gone quiet is named", levels.stalled_sides(rec) == ["voice"])

    steady = {"log": deque(maxlen=8)}
    for _ in range(5):
        levels._heard(steady, "voice")
    check("a capture keeping up is left alone", not steady.get("gaps"))

    # Everything above is arithmetic. Whether the silence lands in the right place is
    # a question about a filtergraph, and the fake ffmpeg in this file cannot answer
    # it — so the real one is asked directly when the machine has it. Skipped rather
    # than failed without it: the rest of this suite is meant to run anywhere.
    real_ffmpeg, real_ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not (real_ffmpeg and real_ffprobe):
        check("filling the gap needs a real ffmpeg, which is not here", True)
    else:
        async def run(cmd: list[str]) -> str:
            done = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await done.communicate()
            return out.decode("utf-8", "replace")

        async def seconds(path: Path) -> float:
            return float((await run([real_ffprobe, "-v", "error", "-show_entries",
                                     "format=duration", "-of", "csv=p=0", str(path)])).strip())

        async def hole(path: Path) -> tuple[float, float]:
            """Where the quiet stretch in a file starts and ends."""
            text = await run([real_ffmpeg, "-v", "error", "-i", str(path), "-af",
                              "silencedetect=n=-50dB:d=0.3,ametadata=print:file=-",
                              "-f", "null", "-"])
            starts = re.search(r"silence_start=([\d.]+)", text)
            ends = re.search(r"silence_end=([\d.]+)", text)
            return (float(starts.group(1)) if starts else -1.0,
                    float(ends.group(1)) if ends else -1.0)

        # What a capture that stalled leaves behind: a second of one tone followed
        # straight by the tone that was playing two seconds later. Nothing in the
        # file says the time in between ever happened.
        stalled, padded = TMP / "stalled.wav", TMP / "padded.wav"
        await run([real_ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000",
                   "-f", "lavfi", "-i", "sine=frequency=880:duration=1:sample_rate=48000",
                   "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[o]",
                   "-map", "[o]", "-c:a", "pcm_s16le", str(stalled)])
        check("the stall left a two-second file", abs(await seconds(stalled) - 2.0) < 0.1)
        check("with no quiet in it at all", await hole(stalled) == (-1.0, -1.0))

        cmd = mixing.pad_command(stalled, padded, [(1.0, 2.0)])
        cmd[0] = real_ffmpeg
        text = await run(cmd)
        check("the silence goes back in", padded.is_file(), text[-200:])
        check("the recording is as long as it was open",
              abs(await seconds(padded) - 4.0) < 0.1)
        starts, ends = await hole(padded)
        # The whole point. Length alone would be satisfied by tacking the silence on
        # the end, which gives a file of the right size in which everything said
        # after the stall is still two seconds early.
        check("the silence is where the capture stopped", abs(starts - 1.0) < 0.15, starts)
        check("and what came after it is back where it was said",
              abs(ends - 3.0) < 0.15, ends)

    print("a tap that heard nothing is not a channel")
    silent = {"computer": record.SYSTEM_AUDIO, "ever": set(),
              "sys_pcm": TMP / "quiet.pcm", "voice_wav": TMP / "nothing.wav"}
    silent["sys_pcm"].write_bytes(b"\0" * 480_000)   # five seconds of written-down silence
    check("silence the tap filled in is not a speaker", levels.captured_sources(silent) == [])
    silent["ever"] = {"computer"}
    check("and sound that reached it is", levels.captured_sources(silent) == ["computer"])
    del silent["ever"]
    check("a recording rescued from a crash is not second-guessed",
          levels.captured_sources(silent) == ["computer"])

    print("work dir sweep")
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    stale, fresh = config.WORK_DIR / "stale", config.WORK_DIR / "fresh"
    stale.mkdir(exist_ok=True)
    fresh.mkdir(exist_ok=True)
    os.utime(stale, (0, 0))
    jobs.sweep_work_dirs()
    check("stale scratch removed", not stale.exists())
    check("live scratch kept", fresh.exists())

    print("a stall is noticed on the path macOS actually uses")
    # The live "gone quiet" warning read rec["moved"], which only _drain — the
    # ffmpeg reader — ever wrote. On macOS the Swift helper captures both sides,
    # so nothing on this machine could trip it: a tap that died 57 seconds into a
    # 132-second recording was never mentioned. Found by ear, twice.
    stalling = {"log": [], "moved": {"computer": time.monotonic() - 5.0},
                "heard": {"computer": 3.0}}
    levels._heard(stalling, "computer", already_padded=True)
    check("a helper side that went quiet is recorded as a stall",
          [side for side in stalling["stalls"]] == ["computer"])
    check("and it says so in the log", any("handed over nothing" in line
                                           for line in stalling["log"]))
    # The helper keeps its own clock and writes the silence it missed as it goes.
    # Recording a gap here would have _pad_gaps insert that silence a second time
    # and push everything after it out of place.
    check("but no padding is asked for, because the helper already did it",
          "gaps" not in stalling)
    ffmpeg_side = {"log": [], "moved": {"voice": time.monotonic() - 5.0}, "heard": {"voice": 3.0}}
    levels._heard(ffmpeg_side, "voice")
    check("an ffmpeg side still asks to be padded", "gaps" in ffmpeg_side)
    check("and both routes agree it stalled", "stalls" in ffmpeg_side)
    check("the warning can now see a helper side",
          levels.stalled_sides({"moved": {"computer": time.monotonic() - 5.0}}) == ["computer"])

    print("a capture that is losing audio says so while it is losing it")
    # The fault this covers cost a two-hour client meeting. The computer side ran
    # at 14-28% padding for half an hour — silence written in place of audio that
    # never arrived — and came back as gibberish transcribed as the wrong
    # language. Nothing said a word, because the only message that existed fired
    # for a single gap of two seconds or more and every piece of this was under a
    # millisecond. Measured against 0.1% on the microphone captured by the same
    # process at the same time, so the threshold is not a guess.
    check("a side losing a third of itself is named",
          levels.padded_sides({"computer": {"fraction": 0.33}}) == ["computer"])
    check("and one losing a seventh, which is what the meeting ran at",
          levels.padded_sides({"computer": {"fraction": 0.142}}) == ["computer"])
    check("ordinary jitter is not", levels.padded_sides({"voice": {"fraction": 0.001}}) == [])
    check("nor is a side with nothing to report", levels.padded_sides({}) == [])
    check("the threshold sits above what a healthy capture does",
          levels.TOO_MUCH_PADDING > 0.001)
    check("and below what a broken one did", levels.TOO_MUCH_PADDING < 0.142)
    # Parsed from the helper's own words, which is the only place it is known.
    line = "syscapture: computer padding 12.3s of 45.6s (27%)"
    found = syshelper.HELPER_PADDING.search(line)
    check("the helper's padding report is understood", found is not None)
    check("with the side it speaks for", found and found.group(1) == "computer")
    check("and both numbers", found and (float(found.group(2)), float(found.group(3)))
          == (12.3, 45.6))
    check("a level line is not mistaken for one",
          syshelper.HELPER_PADDING.search("syscapture: computer level -21.0 frames 4800") is None)

    print("what a recording writes down about itself")
    # Every fault so far was diagnosed by hand from the file afterwards, because
    # what the app knew at the time was thrown away. These are the questions that
    # actually cost time.
    quiet_then_loud = [-120.0] * 20 + [-30.0] * 50 + [-120.0] * 30
    check("silence at the ends is not a hole", diagnostics.holes(quiet_then_loud) == [])
    broken = [-120.0] * 10 + [-30.0] * 20 + [-120.0] * 15 + [-30.0] * 20 + [-120.0] * 10
    found = diagnostics.holes(broken)
    check("a silence with sound on both sides is", len(found) == 1, str(found))
    check("timed from where it starts", found[0]["at"] == 3.0, str(found))
    check("and measured", found[0]["seconds"] == 1.5, str(found))
    check("a blink between two sounds is not worth reporting",
          diagnostics.holes([-30.0] * 10 + [-120.0] * 2 + [-30.0] * 10) == [])
    check("nothing at all has no holes to report", diagnostics.holes([-120.0] * 40) == [])

    saved = {"id": "abc123", "path": str(TMP / "nope.m4a"), "started_at": 1.0,
             "status": "saved", "devices": ["0", "system"], "voice": "0",
             "computer": "system", "device_names": {"voice": "Built-in", "computer": "system"},
             "output_device": "SpeakerUID", "sources": ["voice", "computer"],
             "labels": ["Me", "Them"], "log": ["# something happened"],
             "stalls": {"computer": [(3.0, 14.2)]}, "helper_code": 0, "helper": "/x"}
    told = diagnostics.about(saved)
    check("the devices are written by name as well as by id",
          told["devices"]["computer"]["name"] == "system"
          and told["devices"]["voice"]["name"] == "Built-in", str(told["devices"]))
    check("which output the tap was built on is kept",
          told["output_device"] == "SpeakerUID")
    check("so are the stalls seen while recording",
          told["stalls_seen"] == {"computer": [[3.0, 14.2]]}, str(told["stalls_seen"]))
    check("and the log, which used to die with the scratch directory",
          told["log"] == ["# something happened"])
    was_file = diagnostics.RECORDINGS
    try:
        diagnostics.RECORDINGS = TMP / "recordings.jsonl"
        diagnostics.remember(saved)
        check("it survives a round trip", diagnostics.recent()[0]["id"] == "abc123")
        for n in range(30):
            diagnostics.remember({**saved, "id": f"n{n}"})
        diagnostics.trim(keep=10)
        check("and the file does not grow without limit", len(diagnostics.recent(100)) == 10,
              str(len(diagnostics.recent(100))))
        # A diagnostic that can lose somebody's meeting is worse than none.
        diagnostics.remember({"path": object()})     # not serialisable on purpose
        check("an unwritable record is swallowed rather than raised", True)
    finally:
        diagnostics.RECORDINGS = was_file

    print("a recording keeps its two speakers however it is transcribed")
    # The fault this covers shipped for weeks and hid in plain sight: two-track
    # jobs were only ever built by the recorder's own auto-transcribe, which is
    # off by default. Everything else — the Transcribe view, a watched folder —
    # made a one-track job, so a stereo recording was flattened to mono and the
    # feature the whole app exists for silently did not happen.
    stereo = TMP / "2026-01-01 09.00.m4a"
    mono = TMP / "2026-01-01 10.00.m4a"
    theirs = TMP / "some album track.m4a"
    # Real files and a real ffprobe: the whole question is how many channels a file
    # has, and a stub asked that can only give back what the test already assumed.
    real_ffmpeg, real_ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if real_ffmpeg and real_ffprobe:
        for path, channels in ((stereo, 2), (mono, 1), (theirs, 2)):
            subprocess.run([real_ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                            "-ac", str(channels), "-c:a", "aac", str(path)], check=True)
        config.save_settings({"record_label_voice": "Me", "record_label_computer": "Them",
                              "ffprobe_path": real_ffprobe})
        check("a stereo file is read as two channels",
              await transcribe.channels_in(stereo) == 2)
        check("a mono one as one", await transcribe.channels_in(mono) == 1)
        check("one of our stereo recordings gets a track per speaker",
              [t["channel"] for t in await jobs.tracks_for(stereo)] == [0, 1])
        check("labelled with the names from settings",
              [t["label"] for t in await jobs.tracks_for(stereo)] == ["Me", "Them"])
        check("one of ours that is mono has nobody to tell apart",
              await jobs.tracks_for(mono) == list(jobs.ONE_TRACK))
        check("somebody else's stereo file is left alone",
              await jobs.tracks_for(theirs) == list(jobs.ONE_TRACK))
        check("and a file that is not there is not a recording",
              await jobs.tracks_for(TMP / "2026-02-02 11.00.m4a") == list(jobs.ONE_TRACK))
        config.save_settings({"ffprobe_path": str(TMP / "bin" / "ffprobe")})
    else:
        check("skipped: this machine has no real ffmpeg and ffprobe", True)
    # The two ways a job gets its tracks must not disagree. `enqueue` decides from
    # what actually captured and `tracks_for` from the finished file; on a
    # recording with both sides they have to reach the same answer, or a recording
    # transcribed now and the same one transcribed later come back different.
    check("both routes to a two-track job agree",
          jobs.two_tracks(("Me", "Them")) == [{"channel": 0, "label": "Me"},
                                              {"channel": 1, "label": "Them"}])

    print("how far the talking sits above the room")
    # The threshold is not a taste. It sits between the last signal-to-noise ratio
    # that cost nothing (20.5 dB: 72.6s of speech found, 219 words) and the first
    # that cost something (15.7 dB: 61.3s, 187 words), on the same 90 seconds of
    # real speech transcribed ten times. Below that it collapses — 11.1 dB produced
    # one word. If somebody moves this number, these are the measurements to redo.
    check("the warning sits below what was measured as harmless", levels.LOW_SNR <= 20.5)
    check("and above the first ratio that lost words", levels.LOW_SNR > 15.7)
    check("a voice close to the room is called out",
          levels.noisy_sides({"voice": 13.6, "computer": 88.0}) == ["voice"])
    check("a comfortable one is not", levels.noisy_sides({"voice": 23.5}) == [])
    # The measured worst and best of the recordings this app has actually made.
    check("the worst real recording would have been warned about",
          levels.noisy_sides({"voice": 13.6}) == ["voice"])
    check("the median one would not", levels.noisy_sides({"voice": 23.5}) == [])
    # A side that carried nothing has no ratio, and must not be reported as a noisy
    # one: it already has its own message, and two would contradict each other.
    check("a side with no ratio is left alone", levels.noisy_sides({"voice": None}) == [])

    print("keeping only the last few recordings")
    # Deleting somebody's meetings is the one thing here with no undo, so this
    # asks what must never happen rather than only what should.
    shelf = TMP / "keeping"
    shelf.mkdir(exist_ok=True)
    made = []
    for n, stamp in enumerate(["2026-07-28 09.15", "2026-07-29 11.00", "2026-07-30 14.30",
                               "2026-07-31 16.45", "2026-08-01 10.05"]):
        f = shelf / f"{stamp}.m4a"
        f.write_bytes(b"audio")
        os.utime(f, (1000 + n, 1000 + n))    # oldest first, in the order written
        made.append(f)
    theirs = [shelf / "holiday.m4a", shelf / "2026-07-28 09.15.txt",
              shelf / "interview 2026-07-28 09.15.m4a", shelf / "notes.m4a"]
    for f in theirs:
        f.write_bytes(b"not ours")
        os.utime(f, (1, 1))                  # older than everything, and still safe

    check("keeping everything deletes nothing", retention.surplus(shelf, 0) == [])
    check("fewer recordings than the limit deletes nothing", retention.surplus(shelf, 5) == [])
    check("the surplus is the oldest ones",
          [p.name for p in retention.surplus(shelf, 3)] ==
          ["2026-07-29 11.00.m4a", "2026-07-28 09.15.m4a"])
    check("a recording being transcribed is passed over",
          [p.name for p in retention.surplus(shelf, 3, {str(shelf / "2026-07-28 09.15.m4a")})] ==
          ["2026-07-29 11.00.m4a"])
    gone = retention.prune(shelf, 3)
    check("only the surplus went", sorted(gone) == ["2026-07-28 09.15.m4a", "2026-07-29 11.00.m4a"])
    check("the newest are still here", all(f.exists() for f in made[2:]))
    # The one that would be unforgivable: somebody's own files in the folder they
    # chose. A name that merely contains a date is not a name this app produces.
    check("nothing this app did not record was touched", all(f.exists() for f in theirs),
          str([f.name for f in theirs if not f.exists()]))
    check("a folder that is not there is not an error", retention.prune(TMP / "no-such", 1) == [])

    # And that a real save actually calls it. Everything above would pass just as
    # happily if the recorder never asked, which is the failure that leaves a
    # setting on the screen doing nothing at all.
    was_all = config.settings()
    live_folder = TMP / "keeping-live"
    live_folder.mkdir(exist_ok=True)
    old = live_folder / "2020-01-01 08.00.m4a"
    old.write_bytes(b"an old meeting")
    not_ours = live_folder / "wedding.m4a"
    not_ours.write_bytes(b"somebody's own file, in the folder they chose")
    config.save_settings({"recording_folder": str(live_folder), "record_keep": 1,
                          "record_auto_transcribe": False})
    record.RECORDING = None
    await record.start("0", "1")
    await record.TASK
    fresh = Path(record.public()["path"])
    check("saving a recording clears the surplus", not old.exists())
    check("and keeps the one just made", fresh.exists() and fresh.parent == live_folder, str(fresh))
    check("and still leaves somebody else's file alone", not_ours.exists())
    record.RECORDING = None
    config.SETTINGS.write_text(json.dumps(was_all))

    # And the setting that drives it, through the same resolver as the rest.
    was = config.settings()
    config.save_settings({"record_keep": 10})
    check("the limit is read from settings", config.recording_config()["keep"] == 10)
    config.save_settings({"record_keep": "nonsense"})
    check("an unreadable limit keeps everything rather than deleting wildly",
          config.recording_config()["keep"] == 0)
    config.save_settings({"record_keep": -5})
    check("a negative limit keeps everything too", config.recording_config()["keep"] == 0)
    config.save_settings({"record_keep": 10_000})
    check("an absurd limit is capped, not honoured",
          config.recording_config()["keep"] == config.RECORD_KEEP_CEILING)
    config.SETTINGS.write_text(json.dumps(was))
    check("off unless asked for", config.recording_config()["keep"] == 0)

    # Every recording this app has ever written matches, or the deleting is a
    # no-op nobody would notice. Asked of the name-maker itself, not of a copy.
    check("the pattern matches what this app actually names a recording",
          retention.MINE.match(f"{saving._stamp()}.m4a") is not None, saving._stamp())
    taken = shelf / f"{saving._stamp()}.m4a"
    taken.write_bytes(b"first one this minute")
    second = saving._unique(taken)
    check("and the name a second recording in the same minute gets",
          second != taken and retention.MINE.match(second.name) is not None, second.name)

    print("path validation")
    for bad in ("relative/path.mp3", str(TMP / "nope.mp3")):
        try:
            app.resolve_file(bad, "The media file", "invalid_input_path")
            raise AssertionError(f"FAIL: accepted {bad}")
        except app.HTTPException:
            pass
    check("bad paths rejected", True)
    check("basename traversal stripped", Path("../../etc/passwd").name == "passwd")

    await the_whole_thing()

    print("\nall checks passed")


async def the_whole_thing() -> None:
    """One recording, end to end, against times that are known rather than assumed.

    The whole product in one sentence: a word spoken by somebody at a moment appears
    in the transcript, attributed to them, at that moment. Everything else — the
    capture, the padding, the mixing, VAD, the model, the merge — is machinery in
    service of it, and this is the only check that asks the question directly.

    Everything above runs against fake binaries. This one needs the real ones and a
    real model, so it says out loud when it cannot run instead of passing quietly.
    """
    print("a whole recording, from audio to transcript")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    whisper = shutil.which("whisper-cli")
    tools.find_models.cache_clear()
    models = [m for m in tools.find_models() if "silero" not in m["path"].lower()]
    vad = next((str(p) for p in (Path.home() / "whisper-models").glob("*silero*.bin")), "")
    talker = shutil.which("say") if sys.platform == "darwin" else None
    if not (ffmpeg and ffprobe and whisper and models and vad and talker):
        missing = [name for name, got in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe),
                                          ("whisper-cli", whisper), ("a model", models),
                                          ("a vad model", vad), ("say", talker)) if not got]
        check(f"skipped: this machine has no {', '.join(missing)}", True)
        return

    async def run(cmd: list[str]) -> str:
        done = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await done.communicate()
        return out.decode("utf-8", "replace")

    # Speech, not tones: macOS voice isolation removes a sine wave, and whisper has
    # nothing to say about one either. See docs/TRAPS.md.
    stage = TMP / "whole"
    stage.mkdir(exist_ok=True)
    # Phrases, not single words. With one word per utterance a transcript made of
    # one segment per word is indistinguishable from one made of sentences, so the
    # check could not tell whether the regrouping had happened at all — it passed
    # with the regrouping removed.
    said = {"hello there": (0, "Me", 1.0),
            "good morning": (1, "Them", 7.0),
            "thank you": (0, "Me", 13.0)}
    for word in said:
        await run([talker, "-o", str(stage / f"{word}.aiff"), "-r", "170", word])
        await run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-i", str(stage / f"{word}.aiff"),
                   "-af", f"adelay={int(said[word][2] * 1000)}|{int(said[word][2] * 1000)},"
                          "apad=whole_dur=18", "-ar", "48000", "-ac", "1",
                   "-c:a", "pcm_s16le", str(stage / f"{word}.wav")])
    for channel, words in ((0, ("hello there", "thank you")), (1, ("good morning",))):
        inputs: list[str] = []
        for word in words:
            inputs += ["-i", str(stage / f"{word}.wav")]
        mix = (f"[0:a][1:a]amix=inputs=2:normalize=0[o]" if len(words) > 1 else "[0:a]anull[o]")
        await run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *inputs,
                   "-filter_complex", mix, "-map", "[o]", "-c:a", "pcm_s16le",
                   str(stage / f"ch{channel}.wav")])
    source = stage / "meeting.m4a"
    await run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(stage / "ch0.wav"), "-i", str(stage / "ch1.wav"),
               "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[o]",
               "-map", "[o]", "-c:a", "aac", "-b:a", "160k", str(source)])
    check("the fixture is two channels of the length asked for",
          "channels=2" in await run([ffprobe, "-v", "error", "-show_entries",
                                     "stream=channels", "-of", "default=nw=1", str(source)]))

    # What was really said, measured off the file rather than taken from the delays
    # that were asked for — those drifted by up to 61 ms. TRAPS: verify the artefact.
    truth = {}
    for channel, words in ((0, ("hello there", "thank you")), (1, ("good morning",))):
        found = await run([ffmpeg, "-v", "error", "-i", str(source), "-af",
                           f"pan=mono|c0=c{channel},silencedetect=n=-45dB:d=0.15,"
                           "ametadata=print:file=-", "-f", "null", "-"])
        starts = [float(x) for x in re.findall(r"silence_end=([\d.]+)", found)]
        for word, at in zip(sorted(words, key=lambda w: said[w][2]), starts):
            truth[word] = at
    check("every word was found in the fixture", len(truth) == 3, str(truth))

    out = stage / "out"
    out.mkdir(exist_ok=True)
    was = dict(config.settings())
    try:
        # The smallest model present: find_models sorts largest first, and a 3 GB
        # one turns a check into a coffee break.
        config.save_settings({"ffmpeg_path": ffmpeg, "ffprobe_path": ffprobe,
                              "whisper_cli_path": whisper})
        job = jobs.make_job(str(source), models[-1]["path"], str(out), "whole",
                            language="en", vad_model=vad,
                            tracks=[{"channel": 0, "label": "Me"},
                                    {"channel": 1, "label": "Them"}])
        jobs.JOB = job
        await jobs.run_job(job)
    finally:
        config.save_settings(was)
        jobs.JOB = None
    check("the run finished", job["status"] == "completed", str(job.get("error")))

    lines = transcribe.parse_srt(out / "whole.srt")
    check("every line is inside the recording",
          all(0 <= start <= end <= 18_500 for start, end, _ in lines), str(lines))
    # One line per thing somebody said. Without the regrouping this is one line per
    # word, and every assertion below about "the phrase" fails on the first half of it.
    check("a phrase is one line, not one line per word",
          len(lines) == len(said), str([t for _, _, t in lines]))
    for phrase, (_, who, _asked) in said.items():
        first, last = phrase.split()[0], phrase.split()[-1]
        hits = [(start, text) for start, _, text in lines if first in text.lower()]
        check(f"{who} saying '{phrase}' is in the transcript once", len(hits) == 1, str(lines))
        start, text = hits[0]
        check(f"and all of it is on that one line", last in text.lower(), text)
        check(f"and attributed to {who}", text.startswith(who + ":"), text)
        # Half a second: the measured error of this design is under 60 ms, and the
        # fault it replaces was out by eleven seconds.
        check(f"and placed within half a second of {truth[phrase]:.2f}s",
              abs(start / 1000 - truth[phrase]) <= 0.5,
              f"{phrase}: transcript {start / 1000:.2f}s, said {truth[phrase]:.2f}s")
    check("and the two speakers interleave rather than stacking",
          [t.split(":")[0] for _, _, t in lines] == ["Me", "Them", "Me"],
          str([t for _, _, t in lines]))



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
