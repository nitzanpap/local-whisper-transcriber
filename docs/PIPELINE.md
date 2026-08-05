# The pipeline, end to end

What happens to sound between somebody speaking and a line appearing in a
transcript. Written because the faults this project has spent longest on were all
in the seams between steps, and none of them were visible from inside one step.

**The whole thing in one sentence, which is also the test:**

> A word spoken by speaker S at time T appears in the transcript, attributed to S,
> at time T.

Everything below is machinery in service of that. `test_app.py:the_whole_thing`
asserts it literally, against a file it builds so the answer is known.

---

## The shape

```
  microphone ──┐
               ├─→ syscapture.swift ──→ voice.pcm ────┐
  system tap ──┘        (one process,                 ├─→ ffmpeg ──→ master.wav ──→ .m4a
                         one clock)   ──→ computer.pcm┘   (offline)
                                                                          │
                             ┌────────────────────────────────────────────┘
                             ▼
              per channel:  ffmpeg ──→ audio-N.wav (16 kHz mono)
                                          │
                                          ▼
                              whisper-cli --vad --max-len 1 --split-on-word
                                   │                    │
                            stdout │                    │ stderr
                       one line per word          vad_segment_info
                        (segments-N.txt)           (regions-N.txt)
                                   └────────┬───────────┘
                                            ▼
                                    transcribe.regroup
                                            ▼
                          drop_echo → merge_tracks → write_outputs
                                            ▼
                                      .txt and .srt
```

## Step by step

### 1. Capture — `mac/syscapture.swift`

One process holds **both** sides, which is the whole design. `startInput` opens an
input device by UID (the microphone, or a loopback driver like Teams); the process
tap is built afterwards for the system-audio case. Order matters: creating the
tap's aggregate device reconfigures the audio HAL and a capture opened after it
never delivers a sample.

Each side gets a `Sink` writing raw 48 kHz mono s16 — `voice.pcm`, `computer.pcm`.
Both are given **second zero at the same instant** and anything arriving before it
is dropped, so neither starts ahead of the other.

`Sink.catchUp` writes silence for the difference between what has been written and
how long the recording has been running, measured on `CLOCK_MONOTONIC`, which keeps
counting while the machine sleeps. This is what makes the file's length a clock.
`pause()` stops counting instead — a pause is time deliberately not recorded.

**Not ffmpeg.** ffmpeg's avfoundation input delivers ~86% of the samples the device
produces. See TRAPS §2.

### 2. Mixing — `record.py:mix_command`

Offline, once both captures have finished, because reconciling two live clocks was
measured destroying the quieter side. Each side is flattened to 48 kHz mono
(`FLAT`) and joined into stereo — voice left, computer right. Then `_save` encodes
to `.m4a` in the recordings folder.

### 3. Becoming a job — `record.py:enqueue`, `jobs.py:make_job`

A two-channel recording becomes a job with two **tracks**, each `{channel, label}`.
The labels are what appear before each line ("Me", "Them").

### 4. Per-track audio — `jobs.py:run_job` → `transcribe.to_wav`

One ffmpeg per track pulls a single channel out of the master and writes
`audio-N.wav` at 16 kHz mono, which is what whisper wants.

### 5. Transcription — `transcribe.whisper_command`, `tools.stream`

```
whisper-cli -m <model> -f audio-N.wav -l <lang> --print-progress \
            --vad --vad-model <silero> --max-len 1 --split-on-word
```

Two streams come back and **both are needed**:

- **stdout** → `segments-N.txt`, one line per word, each with its own time.
- **stderr** → `regions-N.txt`, the `vad_segment_info` lines saying where VAD found
  speech, in the original file's timeline.

`tools.REGION_LINE` routes the second out of the log — one per pause would bury
everything else — and into a file.

### 6. Putting words back into sentences — `transcribe.regroup`

The crux, and where the timestamps are actually decided.

- **Regions are measured.** Accurate to ~100 ms. They bound a sentence.
- **Word times are whisper's estimate.** Good inside a stretch of speech,
  meaningless across a silence it never saw. A word's *end* is especially not to be
  trusted: whisper runs the last word of a sentence to the first word of the next.

So: regions are joined when the gap between them is under `GAP_MS` (somebody did
not actually stop talking); each word is clamped to its region *before* any rule
looks at it; and a bucket flushes when the region changes, when a pause inside a
region reaches `GAP_MS`, or when the span reaches `MAX_SPAN_MS`.

### 7. Merging and writing — `transcribe.drop_echo`, `merge_tracks`, `write_outputs`

`drop_echo` removes lines one channel picked up from the other. `merge_tracks`
prefixes each line with its label and **sorts by start time**, which is what makes
the transcript follow the conversation. `write_outputs` writes `.txt` and `.srt`
and moves them into the output folder — in a thread, because a move into a
TCC-protected folder blocks until a dialog is answered.

---

## Every place a time is created or destroyed

The list worth keeping, because a fault at any one of these looks identical from
the outside — a transcript with plausible numbers in it.

| # | where | what happens | how it has failed |
|---|---|---|---|
| 1 | `Sink.begin` | **time is created** — second zero for both sides | — |
| 2 | `Sink.catchUp` | silence written for time not captured | tap wrote nothing while quiet, cutting every pause out |
| 3 | `Sink.pause` | time deliberately not counted | — |
| 4 | ffmpeg capture (Linux only) | samples arrive, or do not | lost ~14% on macOS; now unused there |
| 5 | `mix_command` | two files joined; equal length assumed | the two used to start 2.84 s apart |
| 6 | `to_wav` | one channel out, resampled to 16 kHz | — |
| 7 | **whisper segments** | **times are estimated** | invented `00:12 → 00:21` in an 18 s file |
| 8 | **`vad_segment_info`** | **times are measured** | were thrown away as noise |
| 9 | `regroup` | the two reconciled | split phrases; trusted an end it should not have |
| 10 | `merge_tracks` | sorted by start | correct throughout; it only ever inherits |
| 11 | `write_outputs` | ms → `hh:mm:ss,mmm` | — |

**Rows 7 and 8 are the only places time is invented rather than carried.** Any
future timestamp fault is almost certainly there, or in row 9 where they meet.

---

## What the test covers, and what it does not

`the_whole_thing` builds a stereo file with `say`, runs the real pipeline, and
asserts placement, attribution and grouping within 500 ms. It covers rows 6–11 and
runs in about 6 seconds.

**It does not cover rows 1–5** — capture cannot be tested without a device and a
person. Those are checked by measurement instead, written up in `RECORDING.md`, and
the way to re-check them is in TRAPS §20: play a known signal and measure what
comes back.

### Checked against a real meeting

Twenty-one minutes of real two-person conversation, 190 lines, transcribed in 141
seconds: no line longer than the cap, none outside the recording, none of zero
length, none overlapping the one above it. Median line 4.2 s.

The two faults that run found — a line with no duration, and lines that overlapped
— were invisible on a tidy fixture. **Build fixtures to prove the rule; run real
recordings to find the rules you did not think of.**

Nor does it check transcription *quality*. It uses words a model will always get
right, on purpose: it is a test of the plumbing, not of whisper.
