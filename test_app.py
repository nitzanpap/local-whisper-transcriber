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
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lwt-test-"))
os.environ["LWT_DATA_DIR"] = str(TMP / "data")

import app  # noqa: E402  (all of these must follow the env var)
import config  # noqa: E402
import jobs  # noqa: E402
import library  # noqa: E402
import record  # noqa: E402
import tools  # noqa: E402
import transcribe  # noqa: E402
import watch  # noqa: E402

# Writes something big enough to look like real captured audio to the recorder,
# which refuses to save a file that is only a header. With -list_devices or
# -sources it prints a device list instead and exits non-zero, as ffmpeg does.
FAKE_FFMPEG = """#!/bin/sh
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
    # The system-audio helper is left out of the fake world on purpose. Pointing at
    # a source that is not there is what stops these checks from invoking a real
    # Swift compiler, and from reporting whichever permissions this particular
    # machine happens to have granted. The tests that want it patch it in.
    record.HELPER_SOURCE = TMP / "no-syscapture.swift"
    record.HELPER_BIN = TMP / "data" / "no-syscapture"
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
    macos = record._parse_avfoundation(printed)
    check("reads the audio half of the mac device list",
          [d["name"] for d in macos] == ["MacBook Pro Microphone", "BlackHole 2ch"], str(macos))
    check("and leaves the cameras out of it", not any("Camera" in d["name"] for d in macos))
    check("indices come back as ffmpeg's own", [d["id"] for d in macos] == ["0", "1"], str(macos))
    linux = record._parse_pulse(printed)
    check("reads pulse sources too", len(linux) == 2, str(linux))
    check("using the human name, not the identifier",
          "Built-in Audio Analog Stereo" in [d["name"] for d in linux], str(linux))
    check("a loopback device is recognised", record.is_loopback({"name": "BlackHole 2ch", "id": "1"}))
    check("so is a pulse monitor",
          record.is_loopback({"name": "Monitor of Built-in", "id": "alsa_output.x.monitor"}))
    check("a microphone is not", not record.is_loopback({"name": "MacBook Pro Microphone", "id": "0"}))
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
    commands = record.capture_commands(rec)
    check("the driverless source needs no ffmpeg of its own", len(commands) == 1, str(commands))
    cmd = " ".join(commands[0])
    check("the microphone is captured on its own", cmd.count("-i ") == 1, cmd)
    check("into a file of its own", "voice.wav" in cmd, cmd)
    check("nothing is mixed while recording",
          "join=" not in cmd and "aresample" not in cmd, cmd)
    check("and nothing is asked of the device",
          "-ar " not in cmd and "channel_layouts" not in cmd, cmd)
    check("it stops by itself", "-t 60" in cmd, cmd)
    check("no microphone means no ffmpeg for it",
          record.capture_commands({**rec, "voice": ""}) == [])
    two = record.capture_commands({**rec, "computer": "1"})
    check("two real devices become two captures, not two inputs", len(two) == 2, str(two))
    check("each writing its own file",
          "voice.wav" in " ".join(two[0]) and "computer.wav" in " ".join(two[1]))

    print("combining the two afterwards, from finished files")
    mix = " ".join(record.mix_command(rec, ["voice", "computer"]))
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
    one = " ".join(record.mix_command(rec, ["voice"]))
    check("one side needs no join", "join=" not in one and one.count("-i ") == 1, one)

    print("remembering a device by what it is, not where it sits")
    listing = [{"id": "0", "name": "WH-1000XM3"}, {"id": "1", "name": "MacBook Pro Microphone"}]
    check("a remembered name finds its device again",
          record.resolve_saved("MacBook Pro Microphone", listing) == "1")
    moved = [{"id": "0", "name": "MacBook Pro Microphone"}, {"id": "1", "name": "WH-1000XM3"}]
    check("and still finds it after the indices move",
          record.resolve_saved("MacBook Pro Microphone", moved) == "0")
    check("a device that is gone is not guessed at",
          record.resolve_saved("Some Old Headset", listing) == "")
    check("an index saved by an older version still works",
          record.resolve_saved("1", listing) == "1")
    check("but not once nothing sits at it",
          record.resolve_saved("7", listing) == "")
    check("the driverless source is not a device to look up",
          record.resolve_saved(record.SYSTEM_AUDIO, listing) == record.SYSTEM_AUDIO)
    check("and a choice is remembered by name",
          record.name_for("1", listing) == "MacBook Pro Microphone")
    check("the driverless one by its own id",
          record.name_for(record.SYSTEM_AUDIO, listing) == record.SYSTEM_AUDIO)

    print("which sides actually recorded")
    (TMP / "voice.wav").write_bytes(b"x" * (record.EMPTY_WAV + 1))
    (TMP / "computer.pcm").write_bytes(b"")
    check("a side that caught nothing is not a channel",
          record.captured_sources(rec) == ["voice"], str(record.captured_sources(rec)))
    (TMP / "computer.pcm").write_bytes(b"x" * (record.EMPTY_WAV + 1))
    check("and both are when both did", record.captured_sources(rec) == ["voice", "computer"])
    (TMP / "voice.wav").unlink()
    check("a side that never started is not one either",
          record.captured_sources(rec) == ["computer"])
    (TMP / "computer.pcm").unlink()

    print("the computer's audio without a driver")
    code, message = record._why_nothing_arrived({**rec, "helper_code": record.HELPER_DENIED})
    check("a refused permission is named, not guessed at",
          code == "insufficient_permissions" and "Screen Recording" in message, message)
    check("and is not blamed on two capture sessions", "Aggregate" not in message, message)

    async def with_helper(granted: bool) -> dict:
        """devices() as it looks on a machine where the helper exists."""
        async def fake(prompt: bool = False) -> dict:
            return {"helper": Path("/x/syscapture"), "granted": granted}
        was, record.system_audio = record.system_audio, fake
        try:
            return await record.devices()
        finally:
            record.system_audio = was

    got = await with_helper(False)
    entry = next((d for d in got["devices"] if d["id"] == record.SYSTEM_AUDIO), None)
    check("system audio is offered as a source", entry is not None, str(got["devices"]))
    check("as a loopback, so it is the computer's side by default", bool(entry and entry["loopback"]))
    check("nobody is told to install a driver", "needLoopback" not in got["advice"], str(got["advice"]))
    check("the permission is asked about instead",
          got["advice"] == ["needScreenRecording"], str(got["advice"]))
    check("nothing to advise once it is allowed",
          (await with_helper(True))["advice"] == [], str(got["advice"]))

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
          transcript[:2] == ["Me: shalom olam", "Them: shalom olam"], str(transcript))
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

    print("the progress bar across two tracks")
    spanned = jobs.make_job(src, model, str(out), "spanned")
    spanned["percent_base"], spanned["percent_span"] = 50.0, 50.0
    await tools.stream([tools.binary("whisper-cli")], spanned, "whisper_failed")
    check("the second track fills the second half", spanned["percent"] == 100.0, str(spanned["percent"]))
    check("track names appear in the job", jobs.track_files(Path("/w"), 1, 2)[1].name == "segments-1.txt")
    check("and not when there is only one", jobs.track_files(Path("/w"), 0, 1)[1].name == "segments.txt")

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
        names = [m["name"] for m in tools.find_models()]
        check("offers real models", "medium" in names, str(names))
        check("never offers the vad model as a transcriber", "silero-v5.1.2" not in names, str(names))
    finally:
        tools.MODEL_DIRS = real_model_dirs
        tools.find_models.cache_clear()

    vad_job = jobs.make_job(src, model, str(out), "vad", vad_model="/models/silero.bin")
    cmd = " ".join(transcribe.whisper_command(vad_job, Path("/tmp/a.wav")))
    check("vad flags only when a model is set", "--vad --vad-model /models/silero.bin" in cmd, cmd)
    plain = " ".join(transcribe.whisper_command(job, Path("/tmp/a.wav")))
    check("no vad flags otherwise", "--vad" not in plain)

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
