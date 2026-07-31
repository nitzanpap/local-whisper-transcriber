# Recording, defined

The PRD deliberately left this open:

> Recording is not finished being defined. It is treated here as a single verb — press it, audio
> arrives — and it is very likely more than that. Which sources, what happens when one dies, what
> a long meeting needs, what the app should do while it waits, what feedback belongs on screen
> during forty minutes of silence.

This is that round. It is written after a day of building and breaking the recording path against
real hardware, so most of what follows is measured rather than reasoned. Where it is reasoned, it
says so.

---

## 0. What recording is today, in one paragraph

Two captures, started separately, mixed afterwards. The microphone is one ffmpeg reading an
AVFoundation device; the computer's own audio is a Swift helper holding a Core Audio process tap,
writing raw mono to a file. Neither writes the file that gets kept: when both have finished, a
third ffmpeg joins them into one stereo master — your voice left, everything else right — and
that is what makes a transcript that knows who said which line. Nothing is mixed while recording,
because reconciling two clocks in real time was measured destroying the quieter side.

## 1. What the day established, and is not worth relitigating

Each of these cost hours and is written down so it costs nobody else any.

**Order is not a detail.** Creating the aggregate device that carries the tap reconfigures the
audio HAL, and an AVFoundation capture opened afterwards never delivers one sample. Measured both
ways, permissions uninvolved: microphone first, still running once the tap exists; tap first, zero
frames for as long as you wait. The captures go up first and the tap waits for the microphone.

**Silence is not refusal, and the difference is unobservable from outside.** A tap on an output
device playing nothing delivers no callbacks at all. A tap that has been refused delivers
callbacks full of zeros — but only once something plays. So nothing in the app can tell a quiet
room from a denied permission by watching. The cure is not cleverer watching; it is making a
sound on purpose and asking whether it comes back, which is what *Check it works* does.

**Permission cannot be asked about without asking.** Core Audio offers no preflight. macOS is
asked at the moment of use, the way any other application asks, and `NSAudioCaptureUsageDescription`
is load-bearing: without it there is no prompt, no row in either pane, and silence forever.

**The two sides arrive at very different levels.** The tap takes the stream before the hardware
volume; the microphone takes the room. Measured in one recording: −20.7 dB against −31.2 dB, with
the speakers at about 15%.

## 2. Which sources

Today: one microphone, plus the computer's audio. Fixed as *your voice* and *everyone else*, which
is what makes the two-channel transcript possible. That framing is right and should not change —
a conversation has two sides, and the product exists to tell them apart.

Two things inside it are not settled.

**Which microphone.** The app guesses the system default and remembers the choice by name. The
name is resolved to an avfoundation index at record time, and indices move when devices come and
go — a stored `1` has already meant two different devices on this machine. The guess is good and
the resolution is the weak point.

**Whether "the computer's audio" should mean everything.** It currently means *everything the
machine is playing* — the meeting, and also music, notifications, a video in another tab. A Core
Audio tap can be scoped to particular processes; the helper already passes an exclusion list, it
is simply empty. Recording only Zoom, Meet and Teams would produce a cleaner second channel and a
better transcript. It would also silently miss anything played through an app nobody thought of.

> **Question for the owner.** Should the computer's side capture everything, or only the meeting
> applications? Everything is honest and occasionally noisy; only-the-call is cleaner and can be
> wrong in a way nobody notices until afterwards.

## 3. What happens when a source dies

Today: it is survived and never reopened. A capture that exits before it was asked to leaves the
other one running — deliberately, because a Bluetooth microphone dropping out used to take the
computer's audio down with it — and the log says so. But nothing tries again, so a headset that
drops at minute four and returns at minute five leaves a hole for the remaining thirty-five.

This is the largest real gap in recording and it has a clear shape:

- A capture that exits while the recording is still going is a fault, not an ending.
- If the device it was using is still listed, start it again, and note the gap in the log.
- Say it on screen while it is happening. The warning already exists for a side that is not
  arriving; a side that *stopped* arriving is the same sentence with a different tense.
- The gap must be filled with silence rather than closed up, or every timestamp after it is wrong.

**Untested and more likely than any of this: sleep and lid-close.** Nobody knows what happens to
either capture when the machine suspends. That is a measurement, not a design, and it should be
taken before anything here is built on.

## 4. What a long meeting needs

Measured: the master WAV costs 0.69 GB an hour. The cap is three hours, which is 2.07 GB. The disk
guard stops a recording when less than 0.4 GB remains, which is about 35 minutes of headroom.

Those numbers are sound and the machinery around them works — a recording that runs out of room is
stopped and kept rather than lost, and a recording whose process dies leaves an orphan that can be
recovered. What is missing is smaller than it looks:

- The three-hour cap is arbitrary. It is also harmless, and §5a says maximum recording length is a
  question nobody should be asked. Leave it, keep it out of the interface, keep it in the file.
- A recovered orphan's duration assumes 48 kHz, and the microphone is recorded at whatever rate it
  offers. The length shown for a recovered recording is therefore wrong. Small, real, worth fixing.
- Nothing tells you how long you *can* still record. On a full-ish disk that is worth knowing
  before a meeting rather than 35 minutes before the end of one.

## 5. What belongs on screen while it records

Most of the PRD's complaint here has since been answered: there is a clock, a size, a level meter
driven by real levels rather than a timer, and a warning when a side stops arriving. Two things
are still wrong.

**The meter shows one number for two sources.** It takes the loudest of both sides, so a
microphone that dies while music plays looks perfectly healthy. Two meters, side by side, labelled
with the two names already in use. This is the cheapest remaining improvement and the one most
directly serving "someone can tell, at a glance, whether their microphone is working".

**Nothing says how long is left**, either of the cap or of the disk.

Beyond that, silence needs no feedback of its own. The clock moves, the meters move, and a
recording of a quiet room is a correct recording.

## 6. What this leaves as work, in order

1. **Two meters instead of one.** Small, and it closes the last part of §7's promise.
2. **Measure sleep and lid-close.** Before designing anything about interruption.
3. **Reopen a source that dies**, filling the gap with silence so timestamps survive.
4. **Fix the orphan duration** to use the rate actually recorded.
5. **Normalise the two channels before transcribing**, or decide not to — see below.
6. **Split `record.py`.** It is 1,309 lines against a project norm of 200–400. Capture, mixing,
   devices, orphans and permissions are five separable things.

## 7. The open questions

These need deciding rather than building, and two of them change what the product is.

**Should the computer's side be everything, or only the meeting?** (§2)

**Should the two channels be levelled before transcription?** They arrive about 10 dB apart by
construction. Each channel is transcribed separately, so this may not matter at all — or a quiet
voice channel may be the one VAD gives up on, which would mean losing half a conversation quietly.
This is measurable and has not been measured.

**Is a recording a file, or a session?** Everything above assumes a recording is one continuous
file with a start and a stop. A meeting that is paused, or interrupted by sleep, or joined late,
suggests something else. This is the question that would change the shape of the code rather than
its details, and it is the one worth answering deliberately before any of the work in §6.
