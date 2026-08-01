# Traps

Everything here cost real time, most of it more than once. It is written for
whoever works on this next — very likely an AI agent — because none of it is
guessable from reading the code, and several entries are things the code looks
correct without.

Ordered by how much they cost.

---

## 1. The absence of a signal is not evidence

This is the trap this project keeps falling into, in a new costume each time.

- A microphone recording digital zero looked exactly like a quiet room.
- A refused audio tap looked exactly like a machine playing nothing: both deliver
  no callbacks at all.
- ffmpeg buffers its output, so a working capture leaves a WAV at nought bytes for
  tens of seconds. That silence was read as failure, and a working recording was
  refused.
- A capture that stalled for 31 seconds looked healthy, because the meter was
  showing the last value it had been handed and nothing was checking when.

Three separate confident diagnoses were wrong for this reason, and in two of them
the owner was right and the machine's silence was meaningless.

**The cure is not more careful watching. It is making the signal yourself.** Play
a tone and ask whether it comes back. That is exactly what *Check it works* does,
and it is why the check plays its own sound instead of waiting for the user to
speak. Every real bug found on 2026-07-31 was found this way; none were found by
reading code.

## 2. Do not capture audio with ffmpeg on macOS

**ffmpeg's `avfoundation` input hands over about 86% of the samples the device
produces.** Steadily, not in bursts. Its own log accounts for every packet it was
given — `994 packets read, 0 decode errors` — with packets holding 512 samples
(10.67 ms of audio) but arriving every 12.07 ms. The samples never reach it.

The same device through Core Audio: **0.990**, where the missing 1% is the moment
before the first buffer.

Ruled out, so nobody re-tests them: `-drop_late_frames` (defaults true, changed
nothing — 0.860 against 0.863), a mislabelled sample rate (ffmpeg and the system
both report 48000), and the metering filters (a bare capture is equally short).

Capture is `mac/syscapture.swift` now, for both sides. ffmpeg still mixes the
finished files at the end, which is offline work on complete files with no clock
to keep up with, and still does everything on Linux.

**The general lesson:** when a measurement says a device is behaving strangely,
measure the same device through a second, independent path before believing
anything about the device.

## 3. A fix can turn an invisible fault into a visible one

`aresample=async=1` was added to keep the timeline honest, and it did. It also
made the sample loss above *audible* for the first time — twelve gaps of 106 ms
in ten seconds, one every 0.83 s — because the holes it faithfully filled were
real holes that had previously been closed up.

Before: continuous-sounding audio, an eighth short, drifting seven minutes an hour
away from the other channel. After: correct timing, audible chopping.

**Ask what the person actually experiences, not only whether the numbers improved.**
A correct fix that makes the product worse is a regression, and the owner is the
one who will hear it first.

## 4. macOS permissions attach to a binary, and a rebuild changes the binary

Every build changes the code hash, so macOS stops recognising grants. The rows in
System Settings go on looking exactly like working ones — the app is *refused
while appearing to be allowed*. `rebuild.sh` resets them deliberately so it asks
again, which is the only honest state.

Consequences that have each bitten:

- **Do not start a recording immediately after `rebuild.sh`.** The permission
  prompts are still being answered while it runs, and the measurement is worthless.
  Do a throwaway recording first, confirm both sides report a level, then measure.
- **The build directory bundle is a second app.** Spotlight indexes
  `target/release/bundle/macos/`, so it appeared beside the installed one in every
  search — a different binary with its own permission identity, one keystroke away
  from being opened instead of the real one. `rebuild.sh` deletes it after
  installing.
- Running the helper from a shell is **not** the same program as the app running
  it. It will be refused, silently, and deliver digital zeros.

## 5. Never write "not found" over a remembered setting

Clearing the microphone grant made the device listing come back empty. The empty
answer was then saved over a good remembered choice — after which every recording
was the computer's side alone, with nothing said on it and no error anywhere.

**A device that cannot be seen at this moment keeps its name.** Absence of a
device in a listing is not a decision by the user.

## 6. Measurement tools have their own limits, and they lie quietly

- **`ebur128` momentary loudness cannot show speech.** It is defined over a 400 ms
  window, and 400 ms is about two syllables. On a tone switching on and off every
  200 ms it reported a flat −24.5 dB from beginning to end and never once dipped.
  A meter fed that is not slow — it is showing the wrong quantity. Peak over 50 ms
  windows tracks the same tone cleanly.
- **macOS Voice Isolation strips non-speech from the microphone.** A 1 kHz test
  tone played through the speakers produced *nothing* in the microphone channel,
  even bandpassed at the tone frequency. Test signals that must survive a
  microphone have to be speech — `say -o words.aiff "tick"` works.
- **`silencedetect` jitters by about ±10 ms.** Fine for locating a gap, not for
  measuring an offset. Use several events and take the mean spacing.

## 7. A request that waits on a human needs a human's patience

*Transcribe a file* in the menu bar opened a picker, transcribed the chosen file
correctly, and looked like a menu item that did nothing — because the reply never
came back. The tray reads the backend over a hand-written socket with a twenty
second timeout, which is right for every call that answers at once and hopeless
for the one that answers when somebody has finished browsing. The socket gave up,
the window was never shown, and the job ran to completion unseen.

**Match the timeout to what is being waited for, not to the transport.** The
backend gives that dialog five minutes, so the caller now waits five and a half.

**And the symptom is worth remembering:** the work happened, correctly, with no
sign of it. When something "does nothing", check whether it did everything and
failed to say so — `/api/state` showed the finished job immediately.

## 8. Build only what is used, and prove the copy took

`tauri build` makes a `.app` and a `.dmg`. `rebuild.sh` installs the `.app` and has
never once touched the disk image — but building the DMG mounts a scratch volume
and opens the familiar drag-me-to-Applications Finder window, which looked enough
like an installer that the owner dragged the app across by hand after **every**
rebuild for a whole session. Nothing asked them to; the copy had already happened.

Worse, a DMG run that leaves its volume mounted makes *every later build fail*, at
a step nothing depends on, with an error pointing at bundling rather than at a
stray `/Volumes/dmg.XXXXXX`. It builds `--bundles app` now — no window, no volume,
half the bundling. `tauri build` still makes both for a real release.

If a build ever fails in `bundle_dmg.sh`, look for a mounted volume first:
`hdiutil info | grep image-path`, then `hdiutil detach /Volumes/dmg.XXXXXX -force`.

**And the copy into /Applications is now checked against the working tree**, not
only for a valid signature. Everything before it can pass while `/Applications`
still holds an older app — it is the one step whose failure looks exactly like
success, and this session spent time testing an app that was not the one just
built.

## 9. Verify the artefact you just made

A test reported a 52.9-second result for a 13.5-second recording. The recording
was correct; `ls -t ~/Recordings` had picked up an *older* file because the new one
had not been written yet. The number was reported before it was checked.

**Confirm the artefact's identity — its name, its timestamp — before drawing a
conclusion from it.** Waiting on a fixed `sleep` and then taking "the newest file"
is not a check. This one was written, then deleted by a careless slice edit while
adding the section above it, then restored — which is itself entry 11's last bullet.

## 10. A test that dies early hides everything behind it

The Linux CI job had been failing for many commits on `" ".join(None)` — the save
panel test asked the machine it was running on, and a Linux box without zenity has
no picker. Because the suite dies at that line, **the 600 checks after it had never
run on Linux at all**, and were hiding two real bugs.

- **State the platform, do not ask the machine.** The picker test now names the
  platform and whether zenity exists, so all three branches are exercised on every
  runner. The zenity branch had never been tested by anything, which is how it came
  to offer a transcript under the name of the settings file.
- **Reproduce CI locally.** `docker run --rm -v "$PWD":/w -w /w python:3.13-slim`
  gave the exact Ubuntu failure in seconds instead of a push-and-wait cycle — and
  then a second run *with ffmpeg installed*, which is what GitHub's image actually
  has, surfaced a second fault the bare container could not.

## 11. Encode the invariant, not the token

A check asserted `"aresample" not in cmd`, written after `aresample=async=1000`
reconciling two live devices destroyed the quieter channel. It was right to exist
and wrong in form: it also banned `aresample=async=1`, which only fills and trims
and is the one thing keeping the timeline honest.

**Ban the dangerous use, not the word.** The check now allows `async=1`, fails on
any stretching value, and says which use is which.

## 12. Translated markup must be parsed before the script that translates it

A Hebrew confirmation came with an English **Yes** and **Cancel** under it. The
keys existed in both dictionaries and `t("confirm.yes")` returned `כן` when asked —
but `applyTranslations()` runs as the scripts load, and the dialog was written
*below* the `<script>` tags, so it did not exist yet when the one pass over the
document happened. Switching language in-session would have fixed it, because that
calls `applyTranslations()` again; loading straight into Hebrew never did.

Two checks now cover the class rather than the instance: nothing carrying
`data-i18n` may appear after the first `<script src=`, and every key the page asks
for must exist in **both** dictionaries. A missing key falls through to English
silently, which is the same bug wearing a different hat.

**And never interpolate a backend word into a translated sentence.** The status of
an interrupted run went straight into `{was}`, so a Hebrew reader got
"…, cancelled." in the middle of their own language. Those words are translated
through `job.was.<status>` now, and the suite checks every status has both.

## 13. This project's own tooling

- **The suite runs against fake `ffmpeg` and `whisper-cli` stubs.** `ffprobe` echoes
  `123.5` for every duration. Any check that needs real audio must locate the real
  binary with `shutil.which` and skip when it is absent — do not assume the fakes.
- **`web/*.js` share one global scope.** A `const` declared twice is a redeclaration
  error that kills every file loaded after it, and checking files one at a time
  cannot see it. `rebuild.sh` concatenates them in page order and parses the result.
  This is how a second `LANGUAGE_NAMES` silently took `app.js` out.
- **`rebuild.sh` is the gate for anything in the desktop app.** Rust tests live
  there rather than in CI, because running them means building Tauri — minutes of
  webkit on a job that otherwise takes seconds.
- **Never edit a running bash script.** Bash reads scripts incrementally, so
  rewriting one mid-run shifts the byte offsets and kills the process at a random
  point. This orphaned an `afplay` that kept playing and a recording that kept
  running. `bash -n` passes before and after and cannot see it.

## 14. Things an agent working here cannot do

- **Screen capture and assistive access are both blocked.** `screencapture` fails
  with "could not create image from display", and System Events refuses with
  "osascript is not allowed assistive access", so nothing visual can be
  self-verified and no window control can be clicked. `tell application "…" to
  quit` does work, and so does reading the backend over HTTP — reach for those. A menu bar icon shipped as
  a black square because of this and the owner had to report it. **If a change is
  visual and cannot be rendered in the browser pane, say plainly that it is
  unverified rather than implying it was checked.**
- **A template menu bar image uses only the alpha channel.** Handing it the app icon
  puts a solid black square in the menu bar, because that is exactly what an opaque
  icon's alpha channel is. `desktop/src-tauri/icons/menubar.py` draws the glyph.
- **A Tauri tray with `show_menu_on_left_click(false)`** looks like a dead icon:
  the menu can then only be reached by right-click, which on a trackpad means two
  fingers, and control-click does not reach it either.

## 15. How to measure this app

The methods that produced every real answer, so they can be reused:

- **Two known signals, one moment.** Play a pattern through the speakers while
  recording both sides. The tap takes it digitally, the microphone acoustically,
  and any difference between the two channels is the captures disagreeing about
  time. Bursts exactly 2.000 s apart gave the drift (1.775 s recorded) and later
  proved it gone (1.995–2.000 s) and the channels aligned to the sample.
- **Let the tool stop itself.** `ffmpeg -t 20` taking 20.36 s of wall clock while
  producing 17.05 s of audio is what separated "the timestamps are wrong" from
  "the samples are missing". Signalling a process and measuring afterwards cannot
  make that distinction.
- **A stalling pipe reproduces a stall.** Feeding raw PCM through a shell that
  sleeps in the middle gives a deterministic gap without needing to sleep a Mac.
- **Compare against a second implementation.** Twelve lines of Swift measuring the
  same microphone through Core Audio is what proved the device was fine and the
  tool was not.

---

## The habit underneath all of it

State what you expect, measure it, and say which parts were verified and which
were inferred — in those words, so the difference is visible. Where a measurement
disagrees with the owner, suspect the measurement first: on this project it has
been wrong more often than they have.

---

## And the map

`docs/PIPELINE.md` traces sound from the microphone to a line in a transcript, and
lists every place a time is created, carried or invented. Two of those places
invent rather than carry — whisper's own segment times, and the VAD regions — and
every timestamp fault this project has had lives at one of them or where they meet.

Read it before changing anything between capture and the `.srt`.

