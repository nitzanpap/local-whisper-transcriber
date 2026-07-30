#!/bin/bash
# Proves the driverless capture path on real hardware, which the test suite cannot:
# it fakes ffmpeg away, and no CI machine has a microphone or a permission to give.
#
# Run it from a Terminal you can click a permission prompt in. It records five
# seconds twice — the computer's audio alone, then the computer's audio and the
# microphone together the way a real recording does it — and says whether sound
# actually arrived in each, because a silent file and a working one are the same
# size and only the levels tell them apart.
#
# Nothing is kept: everything goes in a temporary directory that is removed on exit.

set -u

HELPER="${HELPER:-$HOME/.local-whisper-transcriber/syscapture}"
SECONDS_EACH="${SECONDS_EACH:-5}"

if [ ! -x "$HELPER" ]; then
    echo "No helper at $HELPER."
    echo "Open the Record view once and it will build itself, or:"
    echo "  swiftc -O -parse-as-library -o \"$HELPER\" mac/syscapture.swift"
    exit 1
fi
command -v ffmpeg >/dev/null || { echo "ffmpeg is not on PATH."; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [ -n "${TONE_PID:-}" ] && kill "$TONE_PID" 2>/dev/null' EXIT

echo "== permission =="
# --request rather than --probe: this is the one call allowed to raise the prompt,
# and a first run has never been asked. Allow it, then run this again if macOS
# only applies it to a process started afterwards, which it usually does.
"$HELPER" --request >/dev/null 2>&1
case "$("$HELPER" --probe)" in
    *true*) echo "   granted" ;;
    *) echo "   NOT granted — allow it in System Settings → Privacy & Security →"
       echo "   Screen & System Audio Recording, for this Terminal, then run this again."
       exit 2 ;;
esac

# Something has to be playing or a correct capture is indistinguishable from a
# broken one. A tone through the default output is the least intrusive way.
ffmpeg -hide_banner -loglevel error -f lavfi \
       -i "sine=frequency=440:duration=$((SECONDS_EACH * 2 + 4))" -y "$WORK/tone.wav"
afplay "$WORK/tone.wav" & TONE_PID=$!
sleep 1

# Reports the loudest sample in a file. -91 dB is ffmpeg's way of saying silence.
level() {
    ffmpeg -hide_banner -nostdin -i "$1" -af volumedetect -f null - 2>&1 |
        sed -n 's/.*max_volume: \(.*\)/\1/p' | tail -1
}

verdict() {
    local what="$1" file="$2" peak
    peak="$(level "$file")"
    echo "   $what: peak ${peak:-unknown}, $(stat -f%z "$file" 2>/dev/null || echo 0) bytes"
    # -91 dB is the quietest a 16-bit sample can be and still not be zero, so
    # anything at or below -90 is silence however it was worded.
    if [ -z "$peak" ] || awk -v p="${peak%% *}" 'BEGIN { exit !(p <= -90) }'; then
        echo "   FAILED — nothing audible arrived"
        return 1
    fi
    # A level says something arrived, not that it arrived whole. Dropouts inserted
    # by a timestamp problem leave audio at a healthy peak and still destroy it:
    # the giveaway is many short silences of near-identical length, which real
    # sound does not produce. Checking only the level once let exactly that through.
    #
    # The threshold has to follow the signal rather than sit at a fixed floor. A
    # channel holding nothing but room noise hovers around any fixed level and
    # crosses it constantly, which reads as chopped when it is merely quiet — a
    # false alarm this script raised on its own first attempt at the check.
    local floor
    floor=$(awk -v p="${peak%% *}" 'BEGIN { printf "%.0f", p - 25 }')
    if awk -v p="${peak%% *}" 'BEGIN { exit !(p < -40) }'; then
        echo "      too quiet to judge continuity, so not judged"
        echo "   OK (level only)"
        return 0
    fi
    local gaps per_second
    gaps=$(ffmpeg -v info -nostdin -i "$file" -af "silencedetect=noise=${floor}dB:d=0.1" -f null - 2>&1 |
           grep -c 'silence_start')
    per_second=$(awk -v g="$gaps" -v d="${SECONDS_EACH:-5}" 'BEGIN { printf "%.1f", g / d }')
    echo "      $gaps silence runs below ${floor} dB (${per_second}/s)"
    if awk -v p="$per_second" 'BEGIN { exit !(p >= 1.0) }'; then
        echo "   FAILED — chopped into pieces, not a continuous recording"
        return 1
    fi
    echo "   OK"
}

# The same filter record.py puts on every input. Not decoration: wallclock
# timestamps start at the current time, so without first_pts=0 rebasing them to
# zero, -t sees a stream that began 1.8 billion seconds ago and writes a header
# and nothing else. Getting this wrong here once looked like a capture failure.
ONE_STREAM="aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono,aresample=async=1000:first_pts=0"

echo "== the computer's audio on its own =="
mkfifo "$WORK/sys.pcm"
"$HELPER" "$WORK/sys.pcm" & HELPER_PID=$!
ffmpeg -hide_banner -nostdin -loglevel error -y \
       -f s16le -ar 48000 -ac 1 -use_wallclock_as_timestamps 1 -i "$WORK/sys.pcm" \
       -af "$ONE_STREAM" -t "$SECONDS_EACH" -c:a pcm_s16le "$WORK/system.wav"
kill -INT "$HELPER_PID" 2>/dev/null; wait "$HELPER_PID" 2>/dev/null
ONE=0; verdict "system audio" "$WORK/system.wav" || ONE=1

echo "== both together, the way a recording does it =="
# The microphone by name rather than by index, because indices move between runs.
MIC="$(ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 |
       sed -n 's/^\[AVFoundation[^]]*\] *\[\([0-9]*\)\] \(.*Microphone.*\)$/\1/p' | head -1)"
MIC="${MIC:-0}"
echo "   microphone is device $MIC"
mkfifo "$WORK/sys2.pcm"
"$HELPER" "$WORK/sys2.pcm" & HELPER2_PID=$!
# The same filter graph record.py builds: each source flattened to mono, drift
# corrected, then joined so voice is the left channel and the computer the right.
ffmpeg -hide_banner -nostdin -loglevel error -y \
       -f avfoundation -i ":$MIC" \
       -f s16le -ar 48000 -ac 1 -use_wallclock_as_timestamps 1 -i "$WORK/sys2.pcm" \
       -filter_complex "[0:a]$ONE_STREAM[voice];[1:a]$ONE_STREAM[computer];[voice][computer]join=inputs=2:channel_layout=stereo[out]" \
       -map "[out]" -t "$SECONDS_EACH" -c:a pcm_s16le "$WORK/both.wav"
kill -INT "$HELPER2_PID" 2>/dev/null; wait "$HELPER2_PID" 2>/dev/null

BOTH=0; verdict "mixed" "$WORK/both.wav" || BOTH=1
# Each channel on its own, because a stereo file with sound in it proves nothing
# about whether both sides are there — and both sides is the whole feature.
ffmpeg -hide_banner -nostdin -loglevel error -y -i "$WORK/both.wav" \
       -filter_complex "channelsplit=channel_layout=stereo[l][r]" \
       -map "[l]" "$WORK/left.wav" -map "[r]" "$WORK/right.wav" 2>/dev/null
LEFT=0; verdict "left, your microphone" "$WORK/left.wav" || LEFT=1
RIGHT=0; verdict "right, the computer" "$WORK/right.wav" || RIGHT=1

echo
if [ "$ONE$BOTH$LEFT$RIGHT" = "0000" ]; then
    echo "All four passed: two sources, kept apart, no driver installed."
else
    echo "Something came out silent. If only the left channel did, it is the"
    echo "microphone permission (Privacy & Security → Microphone) rather than this."
    exit 1
fi
