# What is left

Ordered within each section by what would hurt most to leave. Written after a long session of
building the recording feature and debugging it against real hardware, so several entries name the
evidence rather than a hunch.

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
- [ ] **The two captures lose their relative start offset.** `aresample=first_pts=0` is applied to
      each independently, so if they begin 200 ms apart every cross-channel time is out by that.
      Tolerable for labelling, wrong for overlapping speech. Stamp each start, apply the delta when
      mixing.
- [ ] **Orphan durations assume 48 kHz.** The microphone is recorded at whatever rate it offers now,
      so the length shown for a recovered recording is wrong.
- [ ] **A source that dies is survived but never reopened.** A Bluetooth microphone that drops and
      comes back leaves a hole for the rest of the meeting.

## QA and debugging

- [ ] **Nothing exercises the packaged app.** Four faults reached the installed app because every
      check ran in a browser: `window.confirm` returning falsy in the webview, a missing
      `record.py`, a signature broken by the app's own first run, and the app serving its own copy
      of `web/` so edits appeared to do nothing. `rebuild.sh` now checks the mechanics of a build;
      nothing checks its behaviour.

- [ ] **A recording loses the time it was interrupted for.** Measured: 70 seconds open, 39.2
      seconds saved, 31 seconds gone. Neither capture dies — both pause and resume on their own —
      and the hole is closed up rather than filled, so every timestamp after it is 31 seconds
      early. Nothing on screen says a thing, because the warning asks whether a side is arriving
      and the last level it saw is still there. See docs/RECORDING.md §3.
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
