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
   is the structural change; everything else is easier afterwards.
2. **Recording as a full-screen mode.** Small change, disproportionate effect on how the app feels.
3. **Design the wait.** The longest part of the experience, currently the least designed.
4. **Rewrite every error as a sentence**, with details collapsed.
5. **Reduce settings to three questions** and infer the rest.

## 9. What this document is not

It is not a rescue of a bad design. The visual language here is better than most local-first tools
and should survive intact. What it needs is fewer things on screen at once, one clear subject per
screen, and honesty about what is happening — which are information architecture problems wearing a
visual-design costume.

Restyling first would have been the expensive mistake: it would have produced a prettier version of
the same confusion.
