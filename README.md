# local-whisper-transcriber

A local web UI over `ffmpeg` + whisper.cpp's `whisper-cli`. Pick a file, pick a model,
get `.txt` and `.srt` next to it. Nothing leaves your computer.

## Requirements

`ffmpeg`, `ffprobe`, `whisper-cli` on `PATH`, a whisper.cpp model file, and `uv`.

```bash
brew install ffmpeg whisper-cpp uv
```

## Run

```bash
uv run --script app.py
```

Python dependencies are declared inline in `app.py` (PEP 723), so there is no virtualenv
to create and nothing to install first.

Then open http://127.0.0.1:8765 — loopback only, no CORS, no accounts.

To keep it running for good — starts at login, restarts if it ever dies:

```bash
sed "s|__DIR__|$PWD|g" launchagent.plist > ~/Library/LaunchAgents/com.local-whisper-transcriber.plist && launchctl load ~/Library/LaunchAgents/com.local-whisper-transcriber.plist
```

## The three views

**Transcribe** — pick a file (or several; the rest queue behind the first), watch progress,
cancel or resume. **Library** — everything transcribed so far, searchable across all of it;
open one to read the cues beside the audio, click a line to jump there, and the line being
spoken highlights and follows. **Settings** — the defaults every new job inherits, watched
folders, VAD, and tool paths.

## Watched folders

Point Settings at a folder and anything new inside is transcribed on its own, roughly every
five minutes. It deliberately leaves alone: files modified in the last two minutes (still
being written), files with a transcript already beside them, files already in history, and
anything past 25 files in one sweep. Whatever it skipped is reported, never dropped quietly.
"Queue a folder now…" runs the same scan once, on demand.

## Transcript quality

whisper invents speech during silence. Three minutes of silence from a real meeting came
back as `תודה רבה.` ("thank you very much") six times, once every 30 seconds — identically
with and without `--max-context`. Voice activity detection is the fix. Download the model
once, then set it in Settings:

```bash
curl -L -o ~/whisper-models/ggml-silero-v5.1.2.bin \
  https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
```

Leave the VAD field empty to keep it off.

On `--max-context 64`, inherited untested from the PRD: over three minutes of real Hebrew
speech it changed nothing that matters — 88 segments against 85, the same three repeated
lines, slightly more fragmented phrasing with it than without. Not enough to justify
changing the default, and a three-minute sample cannot test the thing max-context actually
guards against, which is repetition loops on hour-long audio. Left as it is, deliberately.

## If a run is interrupted

Killing the backend no longer costs you the transcription. whisper-cli prints each
finished segment as it goes, so the run is checkpointed continuously; on restart the
page offers **Resume**, which restarts whisper at the last finished segment with
`--offset-t` and skips re-converting the audio. Timestamps stay absolute across the
offset, so the two halves need no stitching — they are one segment stream.

Cancel keeps its progress too. **Discard** throws it away. Anything still resumable
survives seven days in `~/.local-whisper-transcriber/work/`; other scratch is swept
after six hours.
"Choose…" opens the native macOS/`zenity` picker, so paths never have to be typed.

Self-check (fake `ffmpeg`/`whisper-cli`, no model needed, ~5s):

```bash
uv run --script test_app.py
```

## Config

- Models are found automatically: any `ggml-*.bin` under `~/whisper-models`, `~/models`,
  `~/.cache/whisper`, `~/whisper.cpp/models`, `/opt/homebrew/share/whisper-cpp`,
  `/usr/local/share/whisper-cpp`, or the folder of the last model you used. Largest wins
  by default; pick "Somewhere else…" for anything outside those. The scan runs once per
  process, so restart after adding a model to a known folder.
- `~/.local-whisper-transcriber/` — `settings.json` (optional binary path overrides,
  `default_model_path`, `default_language`), `history.jsonl`, `work/` (scratch WAVs).
- `LWT_DATA_DIR`, `LWT_PORT` override the location and port.
- Advanced → extra `whisper-cli` args, default `--temperature 0 --entropy-thold 3.0
  --max-context 64`, split into tokens with `shlex` and passed as an argv array.

## Deliberate omissions vs. the PRD

- **No chunking, no SRT merger.** `whisper-cli` streams a file of any length in
  bounded memory and emits correct timestamps for the whole thing, so the chunk
  manifest, offset arithmetic, block renumbering and per-chunk state have no job to
  do. `--print-progress` supplies the progress percentage that chunking was there to
  approximate. Ceiling: cancelling a 3-hour job loses the whole run. Add chunking (and
  with it, resume) when that actually bites.
- **No SQLite, no repositories, no migrations.** History is an append-only JSONL file;
  in-flight job state is one dict. Add a database when there are concurrent jobs to
  coordinate.
- **No SSE, no event schema, no event IDs.** The UI polls `GET /api/state` once a
  second and gets the whole world back. Reconnect-after-refresh is free — it is just a
  GET. Add streaming if a poll ever costs something.
- **No React/Vite/Tailwind/npm.** Three static files in `web/`, no build step.
- **No chunk-level resume machinery.** Resume needs no chunking: whisper-cli's
  `--offset-t` emits absolute timestamps, so restarting at the last finished segment
  continues the same stream. Outputs are written from that segment stream rather than
  by `-otxt`/`-osrt`; verified byte-identical to whisper's own writers except that the
  leading space on each line is stripped.
- **No versions in the environment check** — resolved path plus the executable bit is
  what the code acts on.

Kept because removing them would be a bug, not a simplification: argv arrays and never
a shell, loopback binding, path validation, `basename` traversal stripping, explicit
overwrite consent, process-group kill on cancel, outputs written to a work directory
and only moved into place once complete, transcript text kept out of the log,
`ffmpeg`/`whisper` stderr drained continuously.
