# Local Whisper Transcriber

Record a meeting and turn it into a transcript, on your own machine. Press record, or pick a
file, or point it at a folder and forget about it — and get a `.txt` and a `.srt` next to the
recording.

When it does the recording it captures your microphone and your computer's own audio into
separate channels of one file, so the transcript can say **who said which line** without any
speaker-identification model.

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

### As a Mac app (recommended)

```bash
cd desktop && npm install && npx tauri build
```

That produces **`Local Whisper Transcriber.app`** in
`desktop/src-tauri/target/release/bundle/macos/` — drag it to `/Applications`. There is also
a `.dmg` next to it if you want to move it to another Mac.

Open it like any app. It starts the transcriber, shows the interface in its own window, and
**stops everything when you quit** — including if you force quit it. Nothing of this app
runs when its window is closed, and nothing starts at login unless you ask it to.

Building it needs Rust and Node (`brew install rust node`); running the built app does not.

### Or from the terminal

```bash
uv run --script app.py
```

Then open **http://127.0.0.1:8765**. `Ctrl-C` stops it.

Dependencies are declared inside `app.py` (PEP 723), so `uv` installs them on first run —
there is no virtualenv to create and no `pip install` step.

### Or always, in the background

Only if you want it running whether or not you asked. It starts at login and restarts if it
crashes — which also means it can transcribe on its own, spending GPU and memory while you
are doing something else. Most people should not want this.

```bash
sed "s|__DIR__|$PWD|g" launchagent.plist > ~/Library/LaunchAgents/com.local-whisper-transcriber.plist
launchctl load ~/Library/LaunchAgents/com.local-whisper-transcriber.plist
```

```bash
launchctl list | grep whisper                                    # is it running?
launchctl kickstart -k gui/$(id -u)/com.local-whisper-transcriber # restart it
tail -f /tmp/local-whisper-transcriber.log                        # what is it doing?
launchctl unload ~/Library/LaunchAgents/com.local-whisper-transcriber.plist  # stop it
rm ~/Library/LaunchAgents/com.local-whisper-transcriber.plist     # and never again
```

Idle it costs about 22 MB of memory and no measurable CPU; the GPU is only touched while a
transcription actually runs.

---

## 3. Use it

The page has four views.

### Record

Two dropdowns — **your voice** and **your computer's audio** — and a button. Your voice goes
into the left channel, everything else into the right, and when you stop, the `.m4a` lands in
your recordings folder and queues itself for transcription.

Because the two are kept apart, the transcript comes out labelled:

```
Me: so where did we land on the pricing page
Them: we agreed to hold it until the redesign ships
Me: right, and that is end of quarter
```

Both dropdowns are optional. One source alone records fine; it just has nobody to
distinguish, so the transcript is unlabelled.

**Your computer's audio needs a permission, not a driver.** macOS offers apps the microphone
and nothing else — there is no input device carrying what your speakers are playing. It will
hand the audio over directly, though, so the second dropdown offers **System audio** and
nothing has to be installed. macOS files that under screen recording and asks first: starting
a recording raises the prompt, and after allowing it you may have to start the app again
before it takes effect. Nothing is captured of your screen — only the audio.

The helper that does this is `mac/syscapture.swift`, about a hundred lines against
ScreenCaptureKit, compiled once on first use into `~/.local-whisper-transcriber/` by the Swift
compiler that comes with Xcode's command line tools. If that compiler is missing the option
simply is not offered, and the older advice below takes its place.

**If you would rather use a loopback driver,** or you are on a Mac too old for the above, that
still works and the Record view still explains it when it finds no other way. In short:

```bash
brew install blackhole-2ch
```

then in **Audio MIDI Setup**, create a **Multi-Output Device** holding your speakers *and*
BlackHole 2ch — built-in output at the top as the clock source, Drift Correction on for
BlackHole, both at 48000 Hz — and select it as your output in System Settings → Sound. You
still hear everything; BlackHole now receives a copy, and this app records from it.

You do **not** need an Aggregate Device, which is where most attempts at this come apart. An
aggregate *concatenates* channels rather than mixing them: a mic plus BlackHole gives a
three-channel device with the mic on channel 3, and a recorder that takes the first two
channels comes back with no microphone at all. Nothing in macOS mixes them for you. This app
opens both devices itself and does the mixing in ffmpeg, which is also what lets it keep them
in separate channels instead of summing them.

First recording will ask for microphone permission. Inside the Mac app that prompt says
*Local Whisper Transcriber*; run from a terminal, your terminal gets asked instead.

A recording stops itself after three hours so a forgotten one cannot fill the disk, and while
it runs the master is a WAV in scratch rather than the final `.m4a` — a WAV's header comes
first, so audio captured before a crash is still playable. If that happens, the Record view
offers it back rather than throwing it away.

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

Split in two: **the basics** at the top — the four things that actually change the result —
and **Expert** below for file paths and flags.

| Setting | What it does |
|---|---|
| **Language spoken in your recordings** | **Check this before a batch** — Hebrew read as English does not fail, it invents. Whatever you used last is remembered. |
| **Quality** | Best / Good / Quick, from the models you have. Bigger is more accurate and slower. |
| **Words it keeps getting wrong** | Names and jargon, so it reaches for them instead of guessing. |
| **Skip silence** | On unless you turn it off. Stops it inventing speech during quiet stretches. |
| **Recording** | Where recordings go, what to call you and everyone else in the transcript, whether to transcribe as soon as one stops, and how long before it stops itself. |
| **Transcript text** | Size and typeface for reading transcripts. |
| **Backup** | Save every setting to a file you choose with a normal Save dialog, or load one back. The app tells you the exact path afterwards. Transcripts are not included — they are already files. |
| **Expert** | Model and silence-model paths, extra `whisper-cli` arguments, and where `ffmpeg` lives. Normally untouched. |

The interface itself is in English or Hebrew — the **EN / עב** switch sits at the top. In
Hebrew the whole layout mirrors. Separately, a transcript of a right-to-left recording is
always laid out right-to-left, whatever the interface language, with the timestamps on the
right where they belong.

---

## 4. Source folders

List the folders you record into — Zoom, Meet, voice memos, wherever. **When you open the
app** it looks once and offers to transcribe anything new:

> **New recordings** — 2 in your source folders have no transcript yet: team-sync.m4a, …
> [Transcribe them] [Not now]

It looks when you open it, and when you press **Check for new recordings now**. Never on a
timer, never while the app is closed. Nothing is transcribed until you say so.

Transcripts go next to each recording by default, or all into one folder if you set one.

The scan deliberately leaves alone:

- files modified in the last two minutes (still being written)
- files that already have a transcript beside them
- files it has transcribed before, even if you deleted the transcript
- the video file when the same folder holds audio (Zoom writes both; the audio is the better
  input and transcribing both is the same words twice)
- anything past 25 files in one sweep

Whatever it skipped is reported, never dropped quietly.

**Transcribe a folder now…** runs the same scan against any folder you choose, one time.

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

**The second recording dropdown offers nothing useful.** **System audio** appears there
whenever the helper can be built, so its absence means no Swift compiler:
`xcode-select --install`. Failing that, a loopback driver still works and section 3 has the
steps; your voice alone records fine until then.

**Recording the computer's audio refuses to start.** The permission has not been given:
System Settings → Privacy & Security → Screen Recording. macOS often only applies it to a
process that starts afterwards, so start the app again before trying once more.

**A recording came out silent, or refused to start.** Almost always microphone permission:
System Settings → Privacy & Security → Microphone. The Record view's process log has whatever
ffmpeg said, and whatever the system-audio helper said alongside it.

**A recording of two sources failed but one alone works.** Some machines will not open two
capture sessions at once. Combine both devices into one **Aggregate Device** in Audio MIDI
Setup and record that as a single source instead. It works, but the channels arrive mixed, so
that transcript has no speaker labels.

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
Scratch is swept after six hours, or seven days if it is still resumable — or if it holds
audio that was recorded but never saved.

Recordings go to `~/Recordings` unless you point Settings somewhere else. They are ordinary
files in an ordinary folder, never inside the dot-directory above.

`LWT_DATA_DIR` and `LWT_PORT` override the location and the port.

---

## 8. Working on it

```bash
uv run --script test_app.py     # ~180 checks, fake binaries, no model needed, ~5s
bash mac/verify-capture.sh      # the one thing the checks cannot fake: real capture
```

The checks fake ffmpeg and whisper-cli away, which is what makes them fast and what lets
them run on a machine with no microphone. The cost is that capture itself is never exercised,
so `mac/verify-capture.sh` exists to do it by hand: it records the computer's audio alone and
then both sources together, and reports the level in each channel, because a silent recording
and a working one are the same size and only the levels tell them apart. Run it from a
terminal that holds Screen Recording permission, and expect to answer a prompt the first time.

| File | What lives there |
|---|---|
| `app.py` | HTTP routes and wiring, nothing else |
| `config.py` | Paths, constants, settings |
| `tools.py` | Finding and running ffmpeg/whisper-cli, the native file picker |
| `record.py` | Capturing microphone + computer audio, and what to do with what was captured |
| `transcribe.py` | Media to segments, segments to txt/srt |
| `jobs.py` | The queue, checkpoints, resume, history |
| `library.py` | Browsing, reading and searching past transcripts |
| `watch.py` | Watched folders |
| `web/` | `index.html`, `app.js`, `record.js`, `library.js`, `settings.js`, `styles.css` — no build step |
| `desktop/` | The Mac app: a Tauri window that owns the backend's lifetime |

The test suite uses fake `ffmpeg` and `whisper-cli` scripts, so it runs in seconds and needs
no model.

---

## Design notes

Things that are deliberately absent, with the reason:

- **No chunking and no SRT merging.** `whisper-cli` handles a file of any length and emits
  correct timestamps for the whole thing. Resume does not need chunks either: `--offset-t`
  keeps timestamps absolute, so a resumed run just continues the same segment stream.
- **No diarisation model.** Speaker labels come from having recorded the two sides into
  separate channels, not from inferring them afterwards. A job carries a list of *tracks*;
  each is converted and transcribed on its own, and the segment streams are interleaved by
  timestamp at the end. That is exact where clustering voice embeddings is a guess, needs no
  extra download, and costs one more whisper pass. It only works for recordings this app
  made — a file that arrived already mixed gets one unlabelled track, as before.
- **No screen video.** It would be the largest part of the file and contributes nothing to a
  transcript, and ffmpeg's screen capture drifts out of sync with audio over an hour. For
  video, Cmd-Shift-5 or OBS alongside this is better than a worse version of both.
- **No bundled audio driver.** Recording the computer's own audio used to need one. It does
  not any more: `mac/syscapture.swift` asks ScreenCaptureKit for the system mix and writes raw
  mono samples into a FIFO that ffmpeg reads as an ordinary input, so nothing is installed and
  the only cost is a permission. A driver is still accepted where the helper cannot be built,
  and the advice for setting one up is still there when it is the only way left.
- **The helper is a pipe, not a second capture device.** Feeding ffmpeg through a FIFO rather
  than opening a second avfoundation session means only the microphone is opened as a device,
  which sidesteps the question of whether a given Mac will open two capture sessions at once.
  The format is stated on ffmpeg's command line because raw samples carry no header, and raw
  is what makes a stream cut short still playable: every prefix of it is valid audio.
- **The helper is compiled on first use, not shipped.** A binary in the repository would need
  signing to be worth trusting and would be another thing to keep current; `swiftc` from
  Xcode's command line tools builds it in a few seconds into the data directory, staged and
  moved so an interrupted build cannot leave something half-written to be trusted later. Where
  there is no compiler the option is not offered rather than failing at the click.
- **The recording master is a WAV, not the .m4a that is kept.** A WAV's header comes first, so
  a recording cut short by a crash is still playable; an `.m4a` missing its trailing index is
  nothing at all. Stopping transcodes the WAV into place and deletes it.
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
