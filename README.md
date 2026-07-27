# local-whisper-transcriber

A local web UI over `ffmpeg` + whisper.cpp's `whisper-cli`. Pick a file, pick a model,
get `.txt` and `.srt` next to it. Nothing leaves your computer.

## Requirements

`ffmpeg`, `ffprobe`, `whisper-cli` on `PATH`, and a whisper.cpp model file.

```bash
brew install ffmpeg whisper-cpp
```

## Run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python app.py
```

Then open http://127.0.0.1:8765 — loopback only, no CORS, no accounts.
"Choose…" opens the native macOS/`zenity` picker, so paths never have to be typed.

Self-check (fake `ffmpeg`/`whisper-cli`, no model needed, ~5s):

```bash
.venv/bin/python test_app.py
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
- **No React/Vite/Tailwind/npm.** One `index.html`, no build step.
- **No resume.** Falls out of the no-chunking decision above.
- **No versions in the environment check** — resolved path plus the executable bit is
  what the code acts on.

Kept because removing them would be a bug, not a simplification: argv arrays and never
a shell, loopback binding, path validation, `basename` traversal stripping, explicit
overwrite consent, process-group kill on cancel, outputs written to a work directory
and only moved into place once complete, transcript text kept out of the log,
`ffmpeg`/`whisper` stderr drained continuously.
