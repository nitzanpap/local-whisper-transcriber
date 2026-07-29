# Local Whisper Transcriber

Turn recordings into transcripts on your own machine. Pick a file — or point it at a folder
and forget about it — and get a `.txt` and a `.srt` next to the recording.

Nothing is uploaded. No account, no cloud, no telemetry. It is a small web page talking to a
local process that runs `ffmpeg` and whisper.cpp's `whisper-cli` for you.

---

## 1. Install what it needs

```bash
brew install ffmpeg whisper-cpp uv
```

Then a whisper model — `large-v3` is the accurate one, about 3 GB:

```bash
mkdir -p ~/whisper-models
curl -L -o ~/whisper-models/ggml-large-v3.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
```

And the voice-activity model, under 1 MB. **Get this one** — without it whisper invents
speech during silence and, worse, replaces quiet talking with it:

```bash
curl -L -o ~/whisper-models/ggml-silero-v5.1.2.bin \
  https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
```

The app finds models in `~/whisper-models` by itself. Nothing to configure.

---

## 2. Run it

```bash
uv run --script app.py
```

That is the whole command. Dependencies are declared inside `app.py` (PEP 723), so `uv`
installs them on first run — there is no virtualenv to create and no `pip install` step.

Then open **http://127.0.0.1:8765**

To stop it: `Ctrl-C`.

### Keep it running always

Better for day-to-day use: install it as a launch agent. It starts at login, restarts if it
ever crashes, and survives closing your terminal.

```bash
sed "s|__DIR__|$PWD|g" launchagent.plist > ~/Library/LaunchAgents/com.local-whisper-transcriber.plist
launchctl load ~/Library/LaunchAgents/com.local-whisper-transcriber.plist
```

Managing it afterwards:

```bash
launchctl list | grep whisper                                    # is it running?
launchctl kickstart -k gui/$(id -u)/com.local-whisper-transcriber # restart it
tail -f /tmp/local-whisper-transcriber.log                        # what is it doing?
launchctl unload ~/Library/LaunchAgents/com.local-whisper-transcriber.plist  # turn it off
```

Re-run the `sed` line whenever you pull changes to `launchagent.plist`.

---

## 3. Use it

The page has three views.

### Transcribe

Choose a file — or several at once, and the rest queue up behind the first. Each transcript
is written next to its own recording. You can queue more while one is running.

While it works you get the stage, a percentage, elapsed time and a log. **Cancel** stops it
and keeps what was transcribed so far, so you can resume later.

### Library

Everything ever transcribed, newest first, searchable across all of it at once. Open one and
the transcript appears beside an audio player: click any line to jump to that moment, and
the line being spoken highlights and scrolls itself into view as it plays. Searching and
clicking a result jumps straight to that sentence in that recording.

### Settings

The defaults every new job inherits.

| Setting | What it does |
|---|---|
| **Model** | Which whisper model to use. Found automatically. |
| **Language** | The language of your recordings. **Check this before a batch** — Hebrew audio transcribed as English comes out as nonsense, and whatever you used last becomes the default. |
| **Extra arguments** | Passed to `whisper-cli` as-is. |
| **Watched folders** | Folders to transcribe automatically. See below. |
| **Vocabulary** | Names and jargon whisper keeps mangling. See below. |
| **VAD model** | Path to the silero model. Empty turns VAD off. |
| **Tool paths** | Only if `ffmpeg` or `whisper-cli` live somewhere unusual. |

---

## 4. Transcribe automatically

Put a folder in **Watched folders** — `~/Documents/Zoom`, say — and anything new that lands
there is transcribed on its own, checked about every five minutes. Finish a call, come back
to a transcript.

It deliberately leaves alone:

- files modified in the last two minutes (still being written)
- files that already have a transcript beside them
- files it has transcribed before, even if you deleted the transcript
- the video file when the same folder holds audio (Zoom writes both; the audio is the better
  input and transcribing both is the same words twice)
- anything past 25 files in one sweep

Whatever it skipped is reported, never dropped quietly.

**Queue a folder now…** runs the same scan once, on demand, and tells you what it would pick
up before you commit to it.

---

## 5. Getting better transcripts

**Use VAD.** Set the VAD model path in Settings. Measured on three minutes of a real meeting:

| | result |
|---|---|
| VAD off | 6 segments, every one an invented `תודה רבה.` ("thank you very much") |
| VAD on | 17 segments of the actual conversation |

It does not merely add noise — without VAD, real dialogue is *replaced* by a hallucinated
pleasantry. On dense speech it costs nothing.

**Fill in the Vocabulary.** Names, jargon, product terms — the words whisper keeps getting
wrong. They are given to the model as context before every window, so the right words are
already in mind. On a real meeting this produced about 10% more transcribed text and the
correct terms started appearing.

Keep it to a couple of lines. whisper truncates a long prompt, and words unrelated to the
recording make the result worse rather than better.

---

## 6. When something goes wrong

**The page says "backend not reachable."** The server is not running. Start it (section 2),
or `launchctl kickstart -k gui/$(id -u)/com.local-whisper-transcriber` if you use the agent.

**A transcription was interrupted.** Nothing is lost. Each finished segment is written as it
happens, so the page offers **Resume**, which restarts whisper where it stopped and skips
re-converting the audio. Cancelled and failed runs keep their progress for seven days;
**Discard** throws it away.

**"missing ffmpeg, whisper-cli."** They are not on `PATH`. Install them, or set the paths in
Settings → Tool paths.

**A model you just downloaded is not listed.** The scan runs once per process — restart it.

**The output is in the wrong language.** Settings → Language. Whatever ran last became the
default.

---

## 7. Where things are

```
~/.local-whisper-transcriber/
├── settings.json     your defaults
├── history.jsonl     every job that has run
└── work/             scratch: converted audio, partial transcripts
```

Transcripts themselves are written next to your recordings and are never touched by cleanup.
Scratch is swept after six hours, or seven days if it is still resumable.

`LWT_DATA_DIR` and `LWT_PORT` override the location and the port.

---

## 8. Working on it

```bash
uv run --script test_app.py     # ~80 checks, fake binaries, no model needed, ~5s
```

| File | What lives there |
|---|---|
| `app.py` | HTTP routes and wiring, nothing else |
| `config.py` | Paths, constants, settings |
| `tools.py` | Finding and running ffmpeg/whisper-cli, the native file picker |
| `transcribe.py` | Media to segments, segments to txt/srt |
| `jobs.py` | The queue, checkpoints, resume, history |
| `library.py` | Browsing, reading and searching past transcripts |
| `watch.py` | Watched folders |
| `web/` | `index.html`, `app.js`, `library.js`, `settings.js`, `styles.css` — no build step |

The test suite uses fake `ffmpeg` and `whisper-cli` scripts, so it runs in seconds and needs
no model.

---

## Design notes

Things that are deliberately absent, with the reason:

- **No chunking and no SRT merging.** `whisper-cli` handles a file of any length and emits
  correct timestamps for the whole thing. Resume does not need chunks either: `--offset-t`
  keeps timestamps absolute, so a resumed run just continues the same segment stream.
- **No database.** History is an append-only JSONL file; the running job is one dict.
- **No SSE or WebSockets.** The page polls once a second and gets the whole state back,
  which also makes reconnecting after a refresh free.
- **No npm, React, or build step.** Five static files.
- **No Docker.** whisper uses Metal on Apple silicon; a Linux VM cannot, and measured here
  that is 4.8× slower (105s of audio: 17.9s with Metal, 85.1s without). It would also lose
  the native file picker.

Kept because removing them would be a bug: argv arrays and never a shell, loopback-only
binding, path validation, explicit consent before overwriting, process-group kill on cancel,
outputs staged in scratch and moved into place only when complete, transcript text kept out
of logs.
