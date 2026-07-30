#!/bin/bash
# Everything that has to be right before a failure can be called a code problem.
#
# Written after several rounds of diagnosing from outputs alone, which produced four
# wrong conclusions in a row: the timestamps, the queue sizes, a stale binary, and a
# script that reimplemented the thing it was testing. None of those were visible in
# the output being read. This prints the state instead of inferring it.
#
# Run it in the same terminal you run the check from — most of what it reports is a
# property of the process asking, not of the machine.

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

say "macOS and toolchain"
sw_vers | sed 's/^/   /'
printf '   ffmpeg      %s\n' "$(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3 || echo MISSING)"
printf '   whisper-cli %s\n' "$(command -v whisper-cli || echo 'NOT ON PATH')"
printf '   swiftc      %s\n' "$(swiftc --version 2>/dev/null | head -1 | sed 's/.*version //;s/ .*//' || echo MISSING)"

say "who is asking — permissions attach to this, not to the machine"
pid=$$
while [ -n "$pid" ] && [ "$pid" != "1" ]; do
    line=$(ps -o pid=,ppid=,comm= -p "$pid" 2>/dev/null) || break
    printf '   %s\n' "$(echo "$line" | awk '{$1="";$2="";print}' | sed 's/^ *//')"
    pid=$(echo "$line" | awk '{print $2}')
done
echo "   The last line is the application macOS judges every permission against."

say "screen recording, as this process sees it"
H="$HOME/.local-whisper-transcriber/syscapture"
if [ -x "$H" ]; then
    printf '   preflight: %s\n' "$("$H" --probe)"
else
    echo "   no helper built yet"
fi
cat <<'NOTE'
   macOS 26 has TWO lists and they are separate grants:
     System Settings -> Privacy & Security ->
       "Screen & System Audio Recording"  — lets the stream start
       "System Audio Recording Only"      — lets audio actually arrive
   A process in the first but not the second gets a stream that runs perfectly
   and delivers silence, with no error anywhere. Check that the terminal you are
   running from appears in BOTH before calling silent system audio a bug.
NOTE

say "audio routing — what there is to capture, and from where"
osascript -e 'set ov to output volume of (get volume settings)
set mt to output muted of (get volume settings)
return "   output volume " & ov & ", muted " & mt' 2>/dev/null || echo "   (volume unreadable)"
# Which devices macOS considers default, which is not the same as which are
# plugged in. A disconnected Bluetooth headset can remain the default output, and
# then everything plays to a device that is not there: nothing is audible and
# nothing reaches the system mix, so a capture of it is correctly silent. That
# accounted for every silent computer channel in a long afternoon of looking.
system_profiler SPAudioDataType 2>/dev/null | awk '
  /^ *[A-Za-z0-9].*:$/ { name=$0; sub(/^ */,"",name); sub(/:$/,"",name) }
  /Default Output Device: Yes/ { print "   default output : " name }
  /Default Input Device: Yes/  { print "   default input  : " name }
' || echo "   (default devices unreadable)"
echo "   If either names a device that is not connected, fix that in Sound before"
echo "   reading anything else here as a fault in the code."
echo "   avfoundation inputs, by the index the app stores:"
ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 |
    sed -n '/audio devices/,$p' | sed -n 's/^\[AVFoundation[^]]*\] */   /p' | grep '^ *\[' | head -8
echo "   Indices move when devices connect or disconnect. The app stores the index,"
echo "   which is a known defect: a stored 1 has already meant two different devices."

say "the helper binary"
if [ -x "$H" ]; then
    src=mac/syscapture.swift
    printf '   binary %s\n' "$(date -r "$H" '+%Y-%m-%d %H:%M')"
    [ -f "$src" ] && printf '   source %s\n' "$(date -r "$src" '+%Y-%m-%d %H:%M')"
    if [ -f "$src" ] && [ "$src" -nt "$H" ]; then
        echo "   STALE — the source is newer than the binary. A crash trace from a line"
        echo "   that no longer exists is what this looks like."
    else
        echo "   up to date"
    fi
fi

say "the app's own settings, which decide what a recording even attempts"
python3 - <<'PY' 2>/dev/null || echo "   (settings unreadable)"
import json, os
p = os.path.expanduser("~/.local-whisper-transcriber/settings.json")
try:
    s = json.load(open(p))
except Exception as e:
    print(f"   no settings file: {e}"); raise SystemExit
for k in ("record_voice_device", "record_computer_device", "default_model_path",
          "vad_model_path", "default_language", "recording_folder",
          "record_auto_transcribe", "whisper_cli_path", "ffmpeg_path"):
    v = s.get(k)
    note = ""
    if k == "record_voice_device":
        note = "   <- an avfoundation index, not a name"
    if k == "vad_model_path" and v:
        note = "   <- VAD discards audio it does not judge to be speech"
    print(f"   {k:24} = {v!r}{note}")
PY

say "the installed app, if the packaged build is what is being tested"
A="/Applications/Local Whisper Transcriber.app"
if [ -d "$A" ]; then
    printf '   signature : %s\n' "$(codesign --verify --deep --strict "$A" 2>&1 || echo 'INVALID — TCC will not honour its grants')"
    printf '   cdhash    : %s\n' "$(codesign -dvvv "$A" 2>&1 | sed -n 's/^CDHash=//p')"
    printf '   mic usage : %s\n' "$(/usr/libexec/PlistBuddy -c 'Print :NSMicrophoneUsageDescription' "$A/Contents/Info.plist" >/dev/null 2>&1 && echo present || echo 'MISSING — macOS cannot prompt, and the Microphone pane has no + button')"
    printf '   record.py : %s\n' "$([ -f "$A/Contents/Resources/backend/record.py" ] && echo bundled || echo 'MISSING — the app has no recording code')"
    if [ -f "$A/Contents/Resources/backend/record.py" ] && ! diff -q record.py "$A/Contents/Resources/backend/record.py" >/dev/null 2>&1; then
        echo "   NOTE: the installed backend differs from this working tree. Testing the app"
        echo "   tests the build, not these files. Rebuild before drawing conclusions."
    fi
else
    echo "   not installed"
fi
echo
