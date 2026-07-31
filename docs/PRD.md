# What this is meant to be

A product document, written after using the thing and finding it good-looking and hard to use.
It argues that almost nothing is wrong with how the app looks and almost everything is wrong with
what it asks of you.

---

## 1. The one sentence

**You had a conversation. Now you have the text of it.**

Everything else — devices, channels, models, languages, silence thresholds, filter graphs — is
machinery that exists to make that sentence true. None of it is the point, and today most of it is
on screen.

## 2. Who this is for

The person it is built for takes meetings and wants to remember them. They know what a microphone
is. They do not know, and must never need to know, what a ggml model is, what voice activity
detection does, what a sample rate is, or which of their four audio devices macOS currently
considers default.

That person is not a beginner. They are an expert at their own job and a stranger to this one. The
current app is written for somebody who has read its source.

There is a second, smaller audience: the person who wants the machinery, because they are tuning
quality or debugging a recording. They are real and worth serving — but serving them on the same
screen as everybody else is what went wrong.

**Decided: the app is designed for the stranger.** Including when the stranger and the author
disagree. The consequence is worth accepting in advance rather than relitigating each time it bites:
controls the author personally likes will be moved behind something, renamed away from what they
are, or decided by the app outright. That is the cost of the thing being usable by somebody who did
not build it, and it is being paid deliberately.

## 2a. The two pains it was built to end

Worth recording, because the app drifted from them and they are the test for every decision.

**Pain one: having the models was not the same as being able to use them.** Every transcription
meant a long command, which became a script, which became a script you hoped you would not close the
terminal on. When something broke it was not obvious that it had. A simple idea — take a recording,
get the text — was manual and tedious every single time.

**Pain two: recording a Mac's own audio alongside a microphone is absurdly hard.** Not hard to want.
Hard to do. This project spent an afternoon proving exactly how hard, and that afternoon is why the
feature exists.

**Both are experience pains, not technical ones.** The models already worked. ffmpeg already worked.
What did not exist was a way to use them that did not cost an evening. That is the whole product:
not transcription, which is solved — the *absence of ceremony* around it.

Which gives the honest one-line purpose:

> **A complete, friendly way to use local models on your own machine to transcribe recordings —
> including ones it records for you.**

"Complete" is load-bearing. A step that sends someone back to a terminal, a Finder window or a
System Settings pane is the original pain returning in a new costume.

## 2b. The two stories everything must serve

**Story A — the meeting.** A call is starting on Zoom, Meet or Teams. Open the app, record, talk.
Afterwards: the transcript, and something usable out of it — copied, exported, and eventually
summarised. Three beats: *record, transcribe, take it away.*

**Story B — the recording that already exists.** A voice memo, a phone recording, a file somebody
sent. There is nothing to capture; it starts at beat two. *Import, transcribe, take it away.*

They converge after the first beat, which is the strongest argument yet that Record and Transcribe
should not be two destinations. They are one story with two openings.

Beat three is the weakest and also the least important, and those are not in tension. It needs to
stop being scattered — one obvious way to copy, one to export, from wherever a transcript is — and
that is all it needs for now. **Summarising is explicitly not a priority.** It is the part most
easily bolted on later and the part least missed today.

The order of investment is: recording, transcription, then everything after. Beat three earns a
tidy-up, not a project.

**And recording is not finished being defined.** It is treated here as a single verb — press it,
audio arrives — and it is very likely more than that. Which sources, what happens when one dies,
what a long meeting needs, what the app should do while it waits, what feedback belongs on screen
during forty minutes of silence. This document does not settle any of that, and should not be read
as though it has. Expect a round of discovery on recording alone once the structure below is in
place.

## 5a. Cost against benefit, for everything

Your framing, and the most useful tool in this document: judge every setting by **what it costs in
attention** against **what it returns**. Temperature is expensive to think about and worth nearly
nothing at the default. Language is nearly free to think about and changes whether the transcript is
usable at all. Those two are not peers and must never look like peers.

**High return, low cost — make these loud.**
Record. Import a file. Language (with *detect* as the default). Copy or export the result. Where the
files went. Whether the microphone is actually hearing anything. Who said which line.

**High return, high cost — invest here until the cost is gone.**
Recording the computer's audio as well as the microphone: enormous value, and today it costs device
choices, two permission prompts and a restart. It should cost one toggle that says *record the
call too*, with everything underneath handled or explained in one sentence.
Accuracy against speed: real value, expressed as *Best / Good / Fast* — never as model filenames.
Which model is installed at all: valuable, and currently a manual download.

**Low return, low cost — keep, but quiet.**
Transcript reading size and face. What you and the other side are called. Which folder recordings go
to. These are pleasant, cheap, and belong behind one disclosure.

**Low return, high cost — remove, or bury and never mention.**
Temperature, entropy threshold, max context, arbitrary extra arguments. VAD model paths. The
locations of ffmpeg, ffprobe and whisper-cli. Maximum recording length. Every one of these is a
question only somebody debugging would ask, and each costs everybody else attention on every visit.

The rule this yields: **anything a person cannot evaluate should not be a question.** If the app
cannot explain in one plain sentence what a setting changes and why they would want it, the app
should decide it.

## 5b. Files as a place, not a side effect

Recordings, transcripts and logs should live in one obvious folder, arranged so that opening it in
Finder makes sense on its own. Dropping a file in should be as good as importing it; taking one out
should not need the app running. The app becomes the pleasant way to work with that folder, not the
only way — and it can then never trap anything.

## 5c. Models as a first-class surface

This runs local models, and that is the reason it exists rather than an implementation detail. Today
that means Whisper large; tomorrow it might mean Parakeet or something not yet released. So models
need somewhere real to live: what is installed, what it is good at, how big, and a way to get another
without a terminal. Presented as capability — *accurate*, *fast*, *this language* — not as files.

## 6a. A few well-aimed animations

Eighty per cent of the benefit comes from a handful, each earning its place:

- The level meter — already real, already measuring, the single most reassuring thing on screen
- Text appearing as it is transcribed, so the wait has a heartbeat
- One considered transition entering and leaving recording mode, because it is a change of state
- A new transcript arriving in the list

That is the budget. Motion should confirm that something happened or that something is alive.
Anything else is noise wearing a nicer coat.

## 6b. Worth studying before inventing

The pains here are ours; the solutions to the sub-problems mostly are not. Worth looking at how each
of these handles the parts we keep rediscovering — permission flows, model management, progress
during a long job, and getting text back out:

- **MacWhisper** — the closest neighbour; how it presents models and quality as choices
- **Handy** (already cloned locally) — how it takes the system default input rather than guessing
- **Aiko** — how far the setting count can be cut before something breaks
- **Descript** — how a transcript becomes something you act on rather than read
- **Superwhisper** — how quality tiers get named for people rather than for files
- **Zoom and Teams** — how a normal application asks for these same macOS permissions

The design thinking should be ours. The implementation patterns should be borrowed on sight.

## 3. What actually happens, in order

1. Something is about to be said — a call starts, or a file already exists.
2. It gets captured.
3. Time passes. **This is most of the experience and the app treats it as an afterthought.**
4. There is text. It is right, or it is wrong in a way that needs explaining.
5. Later, that text has to be found again.

Five steps. The app currently has four top-level views, and none of them is step 3 or step 5.

## 4. What is structurally wrong

Not the aesthetic. The look — warm dark paper, a serif display face, monospaced labels, a single
orange accent — has a real point of view and is worth keeping. The problems are structural, and a
restyle would leave every one of them in place.

**The navigation exposes the implementation, not the journey.** *Record*, *Transcribe*, *Library*
and *Settings* are four peers. But Record and Transcribe are the same activity — getting words out
of audio — arriving by two different doors. Presenting them as separate places makes the first
decision of the app a question about plumbing: *which kind of input do I have?*

**Everything is on at once.** Settings had five equally-weighted controls where two decide the
result. The Record view shows two device dropdowns, an advice panel, a plan line, a level meter and
two buttons before anything has happened. Nothing is deferred, so nothing is emphasised, and the eye
has nowhere to rest. Density reads as complexity even when each individual control is reasonable.

**Failure is silent, then technical.** A recording with a dead microphone looked exactly like a good
one. When something does surface, it surfaces as `malformed_chunk_output` or a process log. The app
knows an enormous amount about why things went wrong and says almost none of it in language anybody
would use.

**Waiting is unowned.** Transcription takes minutes. There is a percentage and a log, but no sense
of what is happening, how long is left, or whether you may leave. The longest part of the experience
has the least design in it.

**Configuration is presented as a peer to the work.** Settings is one of four tabs. In practice it
is opened once, ever.

## 5. What it should be instead

### One place, not four

A single main surface that answers *what do you want words from?* Two ways in, side by side, without
being two destinations:

- **Record** — the primary action, weighted as such
- **A file** — drop it, or choose it

Below that, and always present: **what you have already**. The library stops being a place you
navigate to and becomes the resting state of the app. When nothing is happening, you are looking at
your transcripts. That is what the app is *for*, so that is what it should show.

### Recording as a mode, not a page

Pressing record should take over the screen. Not a panel among panels — a state: large elapsed time,
two live meters, one way to stop. Nothing else visible, because nothing else is relevant while
recording. Coming out of it returns you to your transcripts with the new one at the top, already
working.

### The wait, designed

The minutes after a recording are the app's real proving ground. What belongs there:

- What it is doing now, in words: *listening to your side*, *listening to theirs*, *writing it out*
- An honest estimate, based on the duration and the model, not just a bar
- Permission to leave — it keeps going, and it will be there
- Any text already extracted, appearing as it arrives

### Settings that mostly do not exist

Three questions belong in a preferences window, not a tab: which language, how accurate, where files
go. Everything else — model paths, VAD, extra arguments, vocabulary, binary locations — belongs
behind *Advanced*, and most of it should be inferred rather than asked. The app already finds models
by itself; it should be similarly confident everywhere else.

### Errors in the first person

Every failure the app knows about should be a sentence about what happened and what to do, with the
technical detail available and closed by default:

> **Nothing was recorded from your microphone.** *MacBook Pro Microphone* was selected but no sound
> reached it. Check it is not muted, or choose a different one.
> `Show technical details`

The app already computes most of this — per-channel levels, permission states, device names. It just
does not say it.

## 6. The design language: keep, sharpen, remove

**Keep.** The warm dark paper and single orange accent. The serif display face — it is the most
distinctive thing here and the reason it "looks really good". Monospaced small caps for labels: they
read as instrument markings, which suits an app that is quietly doing signal processing. The
right-to-left support, which is genuinely well done.

**Sharpen.** One thing must dominate every screen. Right now screens have three or four equal
candidates. Spacing should come from a scale, not from eighteen hand-written margins. Numbers that
matter — elapsed time, level, progress — deserve to be *large*, in the display face, treated as the
subject rather than as metadata.

**Remove.** Anything on screen that is not being used right now. Every explanatory paragraph that
could be a tooltip. Every control that is set once. The visual weight currently spent on plumbing.

The aesthetic is a darkroom or a studio instrument: dark, warm, precise, quiet, with one thing
glowing. Lean into that. It is already 70% there and the remaining 30% is subtraction.

## 7. What success looks like

- Someone who has never seen the app records a meeting and gets a labelled transcript **without
  reading anything**.
- Nothing on the first screen requires a decision except *record* or *choose a file*.
- Every failure names a cause and an action in ordinary language.
- Settings is opened once and then never again.
- Someone can tell, at a glance and without waiting, whether their microphone is actually working.

## 8. Where to start

In order, because each makes the next easier:

1. **Collapse Record and Transcribe into one surface**, with the library as the resting state. This
   is the structural change; everything else is easier afterwards, and it follows directly from the
   two stories converging after their first beat.
2. **Reduce settings by the cost-and-return rule.** Mechanical, already decided in §5a, and it makes
   every screen after it cheaper to think about.
3. **Recording as a full-screen mode.** Small change, disproportionate effect on how the app feels.
4. **Design the wait.** The longest part of the experience, currently the least designed.
5. **Rewrite every error as a sentence**, with details collapsed.
6. **Tidy beat three** — one way to copy, one to export, reachable from anywhere a transcript is.

Then stop and reopen recording as its own discovery, before building further on it.

## 9. What this document is not

It is not a rescue of a bad design. The visual language here is better than most local-first tools
and should survive intact. What it needs is fewer things on screen at once, one clear subject per
screen, and honesty about what is happening — which are information architecture problems wearing a
visual-design costume.

Restyling first would have been the expensive mistake: it would have produced a prettier version of
the same confusion.
