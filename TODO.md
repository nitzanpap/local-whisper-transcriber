# What is left

Ordered within each section by what would hurt most to leave. Written after a long session of
building the recording feature and debugging it against real hardware, so several entries name the
evidence rather than a hunch.

## Next, in order

Where things stand after 2026-07-31: recording is, as far as it has been measured,
correct — no sample loss on either side, the two channels aligned to the sample,
silence kept where it belongs and dropped where it was asked for, meters that show
a voice, and a menu bar item. `docs/TRAPS.md` says what was learned getting there
and what not to repeat.

1. ~~**Fix the timestamps a multi-track job produces.**~~ Done 2026-08-01. VAD's placement was
   always accurate; keeping it off two-speaker jobs left whisper to segment a track by itself.
   Now `--vad --max-len 1 --split-on-word`, regrouped in `transcribe.regroup` against the
   `vad_segment_info` regions that were being thrown away as log noise. Measured through the real
   pipeline: words said at 1.006 / 7.019 / 13.061 s now written at 1.010 / 7.070 / 13.100.
   `docs/PIPELINE.md` is the map; `test_app.py:the_whole_thing` asserts it on every run.

2. **Measure the level gap between the two sides.** The tap takes the stream before
   the hardware volume, so the computer's side is near full scale whatever the
   speakers are set to, while the microphone gets whatever the room gives: -20.7 dB
   against -31.2 dB in one recording. Each channel is transcribed on its own so it
   may not matter at all — or a quiet voice channel is the one VAD gives up on,
   which would make it the cause of item 1. Measurable, never measured.
3. **Split `record.py`.** Around 1,600 lines against a project norm of 200-400.
   Capture, mixing, devices, orphans and permissions are five separable things.
   Deliberately held while recording behaviour was still moving; it has stopped
   moving, so this is the moment — but after item 1, not before.

## From reading Handy

Taken from its screens on 2026-08-01, ranked by what they would be worth here. The
project is at ~/devel/tools/Handy; `docs/TRAPS.md` says what of its *code* is worth
copying and what is not.

- [ ] **Group settings, with a note on each row.** Ours is one flat list, which is
      already on this list below as "noisy and flat". Theirs is APP / OUTPUT /
      TRANSCRIPTION / HISTORY, each row a label on the left and one control on the
      right, and an ⓘ beside every label carrying the sentence that would otherwise
      be a hint under it. That last part is the trick: it explains everything and
      clutters nothing.
- [ ] **Keep only the last N recordings.** They have *Auto-Delete Recordings ·
      Keep latest 5* and a *History Limit*. We measured a recording at 0.69 GB an
      hour and have a disk guard that stops a recording when the disk is nearly
      full — which is the emergency, not the policy. A meeting recorder that never
      forgets fills a disk by design.
- [ ] **"Default" as a device, not a device name.** Their microphone dropdown says
      *Default*, so the choice survives plugging in a headset. Ours lists real
      devices and guesses the default when nothing is remembered, so somebody who
      picked one is stuck with it when it is gone. An explicit "Follow the system"
      entry is a better answer than a guess. Relates to TRAPS §5.
- [ ] **Custom words as chips, not a text field.** Ours is a free-text vocabulary
      box; theirs is `Add a word` with a row of removable chips. Same feature,
      obviously better, and a chip cannot be half-typed.
- [ ] **Pick the model from the tray.** Their menu bar item carries a submenu of
      downloaded models with a tick on the active one. Ours has the clock and the
      recording controls; a model submenu is a small addition to `build_tray`.
- [ ] **Copy the last transcript from the tray.** One click after a meeting, no
      window. Fits our menu bar exactly.
- [ ] **Say where the files are, in About.** App data and log directories, each
      with an Open button. Every support question starts there, and ours are not
      written down anywhere a user would look.
- [ ] **Acknowledge what this is built on.** They credit ggml and transcribe.cpp by
      name on the About screen. We ship a Silero VAD model and lean entirely on
      whisper.cpp; that deserves saying, and shipping somebody's model makes it a
      licence question rather than only a courtesy.
- [ ] **Launch on startup, start hidden.** A meeting recorder that has to be
      started before it can catch a meeting is asking to be forgotten. We have the
      menu bar item to be quiet in already.

Not worth copying: their overlay and paste settings (a dictation app pastes into
whatever has focus; we write files), push-to-talk, and Experimental Features as a
toggle.

## Bugs

- [x] **Remove does nothing on a running transcription.** The button called `DELETE /api/queue/{id}`,
      which only ever removed jobs *waiting* their turn, so a job that had started refused silently.
- [x] **A macOS consent dialog freezes the whole app, silently.** `write_outputs` renames into the
      output folder from the event loop, and a rename into a TCC-protected folder — `~/Documents`,
      `~/Desktop`, `~/Downloads` — blocks in the kernel until the dialog is answered. Caught in the
      act: `sample` on the backend showed the main thread parked in `os_rename` → `__rename` while
      `/api/state` answered nothing at all, so the interface went dead mid-transcription with no
      message. An ad-hoc signed build re-prompts after every install, so it is the first run of
      every release rather than a rare path. Both moves into user-chosen folders now run in a
      thread, and the stage already said *Writing transcript*, which is true instead of frozen.
      Confirmed under a real block: `/api/state` answered in 10 ms while the rename sat in a
      worker thread and the main thread waited in `kevent`.

- [ ] **API failures are easy to miss in the interface.** They reach `formError`, which is not
      always on screen. A failed request should be visible wherever the click was.
- [ ] **A quiet channel sorts to the front of a transcript.** A channel carrying continuous sound
      returns one segment spanning the recording — measured at `00:00.000 --> 00:22.040` for a
      sentence spoken around 15–21 s. `silencedetect` finds no regions to trim to even at
      peak-minus-25 dB, and `--max-len` splits it into times that are interpolated rather than
      measured. Needs speech regions located properly and each transcribed with `--offset-t`, which
      `whisper-cli` gives no way to do: it never reports where VAD found speech.
- [x] **The two captures lose their relative start offset.** Was 2.84 s, measured in a quiet room
      from speech played through the speakers exactly two seconds apart. Gone by construction rather
      than corrected: the helper captures both sides in one process and gives them second zero at
      the same instant. Measured again through the app afterwards, the same speech lands at 2.75,
      4.75 … 22.75 in *both* channels.
- [x] **A loopback device chosen as the computer's side was still captured by ffmpeg**, so it lost
      about an eighth of its samples and `aresample` filled the holes with audible silence. It is an
      input device like any other, so the helper takes it: measured over a real recording, that
      channel came back with no gaps at all.
- [ ] **Orphan durations assume 48 kHz.** The microphone is recorded at whatever rate it offers now,
      so the length shown for a recovered recording is wrong.
- [ ] **A source that dies is survived but never reopened.** A Bluetooth microphone that drops and
      comes back leaves a hole for the rest of the meeting. Narrower than it was: a source that
      merely goes quiet — a sleep, or nothing playing — now keeps its place in time, so this is
      only about a capture process that genuinely exits. See `docs/RECORDING.md` §3.

## QA and debugging

- [ ] **Nothing exercises the packaged app.** Four faults reached the installed app because every
      check ran in a browser: `window.confirm` returning falsy in the webview, a missing
      `record.py`, a signature broken by the app's own first run, and the app serving its own copy
      of `web/` so edits appeared to do nothing. `rebuild.sh` now checks the mechanics of a build;
      nothing checks its behaviour.

- [x] **A recording loses the time it was interrupted for.** Measured: 70 seconds open, 39.2
      seconds saved, 31 seconds gone, twice over. Each capture now keeps its own clock and writes
      down the silence it missed. Chasing it found a far larger version of the same fault: the tap
      wrote nothing at all while the machine was quiet, and the microphone lost about an eighth of
      every recording to samples that never arrived — 1.775 s of speech where 2.000 s was played.
      Both fixed and both verified in the packaged app. See docs/RECORDING.md §3.
- [ ] **Nothing tests the app bundle.** Both packaging faults — a missing `record.py`, and a
      signature invalidated by running the app — shipped because CI never builds or opens a bundle.
- [ ] **No automated end-to-end run.** The suite fakes both binaries. Driving `jobs.run_job` over a
      real recording is a handful of lines and would have caught the ordering fault immediately.
- [ ] **The two sides arrive at very different levels.** A Core Audio tap takes the stream
      before the hardware volume, so the computer's side is near full scale whatever the
      speakers are set to, while the microphone is at whatever the room gives: measured
      -20.7 dB against -31.2 dB in the same recording. Each channel is transcribed on its own
      so it may not matter, but a quiet voice channel is the one VAD gives up on.

- [x] **The computer's side has no live meter.** It is written by the helper rather than ffmpeg, so
      nothing measured it in passing. The helper reports its own level once a second now, which
      also tells a refused tap (frames of digital zero) from a quiet machine (no frames at all).

## Product and design

- [ ] **Export is text only.** *Save a copy* writes the transcript's text, because asking which
      of two formats is a question §5a says not to ask. Somebody who wants the `.srt` has *Open
      folder* beside it, which is a worse answer than it sounds if the folder is not obvious.

- [ ] **Settings is noisy and flat.** No grouping, no progressive disclosure, everything at one
      weight. The common path should be nearly empty and customisation deliberately reachable.
- [ ] **Typography hierarchy is weak** — small where it should lead, and the primary action does not
      dominate the screen it is on.
- [ ] **The detected language is never shown.** A transcript that came out wrong gives no clue why.
- [ ] **"VAD found no speech" and "whisper recognised nothing" read identically** despite needing
      different remedies. Both now arrive as sentences with the code folded away, but they are
      still the same sentence, and the remedies are not the same.
- [ ] **README screenshots.** Four slots are marked in `docs/`; they cannot be captured from a
      process without screen-recording permission.

## Architecture

- [x] **The app asks for screen recording and only wants audio.** `syscapture.swift` used
      ScreenCaptureKit, a screen API: it always cost the *Screen & System Audio Recording* grant
      and would not run audio-only, so the helper configured a 2×2 video stream at 1 fps and
      dropped every frame purely to keep audio flowing. Replaced with a Core Audio process tap,
      which is what the separate *System Audio Recording Only* grant exists for. No video, no
      screen permission, and `afplay` is captured now too — a tap listens to the output device
      rather than to applications on a display.

- [ ] **`record.py` is past the size this project keeps to.** Capture, mixing, orphans, permissions
      and device handling could be separate.
- [x] **A level check before recording would beat any device guess.** Done as *Check it works*:
      six seconds, a tone of the app's own, and a verdict per side. Offered on the first screen
      until it passes. What is still missing is the cheaper half — a meter running before the
      button is pressed, so a wrong device is obvious without asking for anything.
- [ ] **Empty `whisper_cli_path` and `ffmpeg_path` resolve through PATH** and work, but read like
      misconfiguration in the state dump.

## Release

- [ ] **Nothing is bundled.** `ffmpeg`, `whisper-cli`, `uv` and the model are all installed
      alongside. Bundling them is what "download and double-click" actually requires, and brings an
      ffmpeg licensing decision that the GPL here already accommodates.
- [ ] **No notarisation.** Without an Apple Developer account every download shows a warning no
      matter how well the app is built.
- [ ] **No release workflow** — no tagging, no version bump, no artifact upload.
- [ ] **CI does not build the desktop app,** so packaging regressions stay invisible until somebody
      installs one.
- [ ] **One moderate Dependabot advisory** on the default branch, untouched and predating this work.

## Chores

- [ ] **The fake ffmpeg leaves a file named `-` in the repository root** on some runs.
- [ ] **No issue templates, contributing guide or changelog.**
- [ ] **Logs are per-job and vanish with the job.** A crash leaves nothing to read afterwards.
