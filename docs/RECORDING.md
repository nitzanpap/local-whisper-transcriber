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

## Where the code is

Split on 2026-08-01, bottom up. Nothing in this list imports `record.py`, and that
is the property to preserve: the moment one of them needs to know about the live
recording, it belongs back in `record.py`.

| file | what it owns |
| --- | --- |
| `syshelper.py` | the Swift capture helper: where it lives, how it is built, the two things it says |
| `devices.py` | what this machine offers to record from, and remembering a choice by name |
| `levels.py` | how loud a thing was, and whether audio is still arriving at all |
| `mixing.py` | the ffmpeg commands, as pure functions of a recording |
| `saving.py` | everything after the audio stops: pad, mix, keep, queue — and reading an orphan back |
| `selfcheck.py` | the test tone, and reading what came back from it |
| `retention.py` | keeping only the last N recordings |
| `record.py` | the one recording that exists at a time, the processes making it, the state it moves through |

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

**The two sides arrive at very different levels, and it does not matter.** The tap takes the stream
before the hardware volume; the microphone takes the room. Measured across every recording with
sound on both sides, the computer's is +17.2 dB louder on average. That gap sat on the list for a
fortnight as a suspected cause of bad transcripts and is not one — §4a has the ladder. What does
matter is the microphone's signal-to-noise, which no amount of gain can improve.

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

## 3. What happens when a source stops

Sources turned out not to die. They go quiet and come back, and until this was fixed the recording
quietly lost the difference.

A capture that really does exit before it was asked to leaves the other one running — deliberately,
because a Bluetooth microphone dropping out used to take the computer's audio down with it — and
the log says so. That part was already right. What was wrong was everything about the far more
common case below.

**Sleep does something worse than killing a capture, and it has now been measured.**

A recording of both sides was left open for 70 seconds of wall clock with a tone playing
throughout, and the machine was put to sleep partway through and woken about half a minute later.
What came back:

| | |
|---|---|
| wall clock the recording was open | 70 s |
| audio in the saved file | 39.2 s |
| missing | 31 s |

Neither capture died. Both processes kept their pids, both stopped producing bytes for the whole
interruption, and both resumed on their own afterwards without being asked. The backend answered
throughout and the status never left `recording`.

So the failure is not a source dying. It is a source **pausing**, invisibly, and the recording
closing the hole up rather than leaving it. A 70-second meeting is saved as a 39-second file, and
every word after the interruption carries a timestamp 31 seconds earlier than when it was said.
Nothing anywhere says so — the live warning cannot fire, because it asks whether a side is
arriving and the last level it saw is still sitting there.

That reshapes the work below. "Reopen a source that dies" was the wrong description of the
problem: nothing dies. What is needed is to notice that a capture has gone quiet for longer than
any pause in a conversation, and to pad the gap with the silence that actually happened so the
timeline survives it.

It was then run a second time, with `caffeinate` stopped in case that was what had kept the
machine up. The same thing happened: 91 seconds open, 54.6 seconds of audio, 36 missing, with a
32-second stretch where the tap produced nothing at all. Different numbers, same shape.

**One honest limit, and it survived two attempts to remove it.** Neither run showed a jump in the
wall clock between consecutive samples, which means the sampling process was never frozen — so
what has been measured, twice, is the audio devices suspending while the machine itself stays up.
`pmset sleepnow` did not fully suspend this Mac either time.

That limit is worth keeping in proportion. A full suspend could differ in exactly one way: the
capture processes might not survive it. If they do not, the recording ends up as an orphan, and
orphan recovery already exists and is already tested. Every other consequence — the missing time,
the collapsed timeline, the silence about it — is the same, and is what the two runs measured.
So the fix does not wait on measuring a full suspend, and chasing one further would be work spent
on the least uncertain part of the problem.

**And then a much larger version of the same fault turned up, in every recording ever made.**

Looking for a way to detect a stalled tap, the tap was run against a machine playing nothing:
**six seconds of quiet produced zero bytes.** Not a fault, and not a sleep — that is simply what a
Core Audio tap does. The output device idles when nothing is playing, no callbacks arrive, and
nothing is written.

Which means the computer's side of every recording had its silences cut out of it. A meeting where
the other person spoke for three minutes, paused for one, then spoke for two, was saved as five
minutes of speech with no pause in it. Sleep was never the bug. Sleep was the largest and most
visible instance of a bug that was firing at every pause in every conversation, and it only came
to light because a nap made it big enough to notice.

### What was done about it

**Each capture now keeps its own honest clock, and fills in what it missed.**

The helper holding the tap measures against `CLOCK_MONOTONIC`, which on Darwin keeps counting
while the machine sleeps, and writes silence for the difference between what it has written and
how long it has been running. It does this on a timer as well as on each callback, so the size of
the file stays a truthful clock rather than becoming one at the end. The same six seconds of quiet
now produce six seconds of file.

The microphone is captured by ffmpeg, which cannot be asked to do that, so it is measured from
outside: `ebur128` reports loudness once for every 100 ms of audio it receives, whether or not
anybody is speaking, so those lines stopping means the capture stopped and nothing else. When they
resume, the gap is the wall-clock time that passed minus the audio that came with it, and it is
recorded along with the position it happened at. Before mixing, one ffmpeg pass puts the silence
back where the hole is.

Both are measured against the wall clock rather than against the device's own timestamps, because
a device whose clock stopped along with it would report that no time had passed at all.

Measured end to end in the packaged app, with the exact pattern the design has to survive — quiet,
then a four-second tone, then quiet:

| | |
|---|---|
| wall clock the recording was open | 11.4 s |
| the saved recording | 11.47 s |
| the tone, in the computer's channel | 3.65 s → 7.65 s, exactly 4.000 s long |

Before the change that channel would have held the four seconds of tone and nothing else, starting
at zero. `pad_command` has a check that fails if the silence is put on the end instead of at the
hole, which is the mistake that would give a file of exactly the right length and every word after
the stall still in the wrong place.

Two consequences worth writing down. The tap now writes about 345 MB an hour whatever the room is
doing, where before a quiet room cost nothing; the existing disk guard covers it. And a channel is
no longer counted as a channel just because its file is large — a tap that never heard anything now
produces a full-length file of silence, and keeping that would mean a stereo master with a dead
side and an hour of digital zero handed to whisper.

**What this leaves, and it is new.** The two captures no longer start at the same instant: the
microphone opens first and the tap is deliberately held back until it is delivering, because
creating the tap's aggregate device kills an AVFoundation capture opened afterwards (§1). Each side
is now internally honest and the two are offset from each other by that wait. Before this change
that offset was lost in a far larger error; now it is the largest one left. It is the next thing to
measure.

## 4. What a long meeting needs

Measured: the master WAV costs 0.69 GB an hour. The cap is three hours, which is 2.07 GB. The disk
guard stops a recording when less than 0.4 GB remains, which is about 35 minutes of headroom.

The guard is the emergency. The policy is `retention.py`: keep the last N recordings and delete the
rest once a new one is safely saved. At 0.69 GB an hour a recorder that never forgets fills a disk
by design, so a limit is the only thing that stops the guard being reached eventually.

It is off unless somebody turns it on. That is not timidity — an upgrade that quietly deleted
meetings would be the one mistake here with no undo, and being on the settings screen afterwards
would not undo it. Three rules make the deleting safe once it is on. Only names this app produces,
so a file somebody put in `~/Recordings` themselves is never a candidate however old it is. Only
the `.m4a`, so the transcript read out of it survives — the Library already draws an entry whose
media has gone. And never a file that is the source of a queued or running job, which would fail
the transcription with something unreadable about a missing file.

It runs when a recording is saved rather than when the setting is changed, so the only deletion
this app performs by itself happens once the thing being kept is already on disk, never while
somebody is still deciding what to set.

Those numbers are sound and the machinery around them works — a recording that runs out of room is
stopped and kept rather than lost, and a recording whose process dies leaves an orphan that can be
recovered. What is missing is smaller than it looks:

- The three-hour cap is arbitrary. It is also harmless, and §5a says maximum recording length is a
  question nobody should be asked. Leave it, keep it out of the interface, keep it in the file.
- A recovered orphan's duration assumes 48 kHz, and the microphone is recorded at whatever rate it
  offers. The length shown for a recovered recording is therefore wrong. Small, real, worth fixing.
- Nothing tells you how long you *can* still record. On a full-ish disk that is worth knowing
  before a meeting rather than 35 minutes before the end of one.

## 4a. The two sides arrive at very different levels, and it does not matter

Measured across every recording this app has made that has sound on both sides:
the computer's side is **+17.2 dB louder on average** — median +15.2, range +5.9 to
+33.9. The tap takes the stream before the hardware volume, so it lands near
-18 LUFS whatever the speakers are set to, while the microphone lands wherever the
room puts it, between -27 and -55.

That gap was on this list for a fortnight as a suspected cause of bad transcripts.
It is not one. The same 90 seconds of real speech, transcribed at eight levels from
-20 to -55 LUFS with nothing else changed:

| level | vad regions | speech found | words |
| --- | --- | --- | --- |
| -20 LUFS | 26 | 75.6 s | 225 |
| -35 LUFS | 26 | 75.6 s | 227 |
| -45 LUFS | 28 | 76.1 s | 224 |
| -55 LUFS | 32 | 74.2 s | 222 |

Words landed within 90 ms of where the loudest run put them, and neighbouring rungs
disagreed on wording as much as distant ones — which is whisper's own run-to-run
variation, not the level.

**What matters is signal-to-noise, and gain cannot fix it.** Turning a quiet channel
up turns its background up with it, which is exactly why the ladder above came back
flat. So the noise was held still and the speech lowered onto it:

| measured snr | speech found | words | same as clean |
| --- | --- | --- | --- |
| 20.5 dB | 72.6 s | 219 | 80% |
| 15.7 dB | 61.3 s | 187 | 68% |
| 11.1 dB | 15.1 s | **1** | 0% |
| 7.2 dB | 0 s | 0 | 0% |

Flat from 52 dB all the way down to 20.5, then it falls off a cliff inside 10 dB.

The microphone channels of real recordings here sit at a **median 23.5 dB** with a
worst of **13.6 dB** — so some were already over the edge, transcribed badly, with
nothing anywhere saying why. The computer's side never is: its floor is digital
zero between sounds, which puts it at 60-105 dB.

`levels.channel_snr` measures it at save time and `LOW_SNR` is 18, set between the
last ratio that cost nothing and the first that cost words. It is a warning, not a
refusal, and it is said at the end of a recording on purpose: this is the last
moment anybody can still move the microphone before the next meeting, and nothing
done to the audio afterwards can add back what was never there.

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

1. ~~**Pad an interrupted capture.**~~ Done, and it turned out to be far more than sleep — see §3.
2. ~~**Say so while it is happening.**~~ Done. `stalled_sides` asks when a level last *moved*
   rather than what it last said, which is the question the old warning could not ask.
3. **Line the two captures up with each other.** New, and now the largest remaining error in a
   recording: the microphone starts before the tap by design, and nothing accounts for the
   difference. Each side is honest about its own length; neither knows where the other began.
   Measure the offset first — it may be under a second, in which case say so and stop.
4. **Two meters instead of one.** Small, and it closes the last part of §5's promise.
5. **Fix the orphan duration** to use the rate actually recorded.
6. **Normalise the two channels before transcribing**, or decide not to — see below.
7. **Split `record.py`.** It is over 1,400 lines against a project norm of 200–400. Capture,
   mixing, devices, orphans and permissions are five separable things.

## 7. The open questions

These need deciding rather than building, and two of them change what the product is.

**Should the computer's side be everything, or only the meeting?** (§2)

**Should the two channels be levelled before transcription?** They arrive about 10 dB apart by
construction. Each channel is transcribed separately, so this may not matter at all — or a quiet
voice channel may be the one VAD gives up on, which would mean losing half a conversation quietly.
This is measurable and has not been measured.

**Is a recording a file, or a session?** Everything above assumes a recording is one continuous
file with a start and a stop. A meeting that is paused, or joined late, suggests something else.
This is the question that would change the shape of the code rather than its details.

Sleep no longer argues for it the way it did — an interruption is now kept as the silence it was,
which is the honest answer for a meeting that carried on in the room while the Mac was not
listening. But a *pause*, asked for on purpose, is a different thing: nobody wants twenty minutes
of silence in the middle of their recording because they stepped out. That is the case still worth
deciding on.
