#!/bin/bash
# Build the Mac app, put it in /Applications, and start it.
#
# Every step it does by hand went wrong at least once when done by hand: a copy that
# silently did not take, so the old code was tested; a bundle missing a backend file,
# because the packaging list is written out by name; a signature invalidated by the
# app's own first run; and a stale server holding the port, so the app started and
# could not bind. Each of those is checked here rather than hoped for.

set -u
cd "$(dirname "$0")" || exit 1

APP="Local Whisper Transcriber.app"
BUILT="desktop/src-tauri/target/release/bundle/macos/$APP"
INSTALLED="/Applications/$APP"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }
say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Something moving while a slow step runs. A build takes the better part of a minute
# and printed nothing at all until it finished, which is indistinguishable from being
# stuck — and the honest response to a script that looks stuck is to kill it.
# Only when a terminal is watching. Piped into a file or a log, the cursor codes are
# not animation, they are literal junk in the middle of the output.
working() {
    label=$1; shift
    if [ ! -t 1 ]; then "$@"; return $?; fi
    "$@" &
    pid=$!
    printf '  %s' "$label"
    while kill -0 "$pid" 2>/dev/null; do
        printf '.'
        sleep 1
    done
    wait "$pid"
    code=$?
    printf '\r\033[K'          # take the dots back, so the tick lands on a clean line
    return $code
}

say "checks first"
run_checks() { uv run --script test_app.py >/dev/null 2>&1; }
working "running the checks" run_checks \
    && ok "the suite passes" \
    || bad "the suite fails — run: uv run --script test_app.py"
for f in web/*.js; do
    node --check "$f" >/dev/null 2>&1 || bad "$f does not parse"
done
# And again as one file, in the order the page loads them. Separate script tags share
# one global scope, so a `const` declared in two of them is a redeclaration error that
# kills every file after it — and checking them one at a time cannot see it. That is
# how a second LANGUAGE_NAMES took app.js down with it while every check passed.
python3 - <<'ORDER' > /tmp/lwt-all.js
import re, pathlib
html = pathlib.Path("web/index.html").read_text()
for name in re.findall(r'<script src="/([\w.]+)"', html):
    print(pathlib.Path("web", name).read_text())
ORDER
node --check /tmp/lwt-all.js >/dev/null 2>&1 \
    || bad "the frontend files clash when loaded together — run: node --check /tmp/lwt-all.js"
ok "the frontend parses, together and apart"

say "building"
build_it() { (cd desktop && npm run build) >/tmp/lwt-build.log 2>&1; }
working "compiling" build_it \
    || { tail -20 /tmp/lwt-build.log; bad "the build failed — full log in /tmp/lwt-build.log"; }
[ -d "$BUILT" ] || bad "the build reported success but produced no bundle"
ok "built"

# Before anything is deleted. A missing bundle here once meant an installed app was
# removed and nothing put back.
codesign --verify --deep --strict "$BUILT" 2>/dev/null \
    && ok "signature verifies" \
    || bad "the built app's signature does not verify"
for f in record.py app.py web/index.html web/app.js; do
    diff -q "$f" "$BUILT/Contents/Resources/backend/$f" >/dev/null 2>&1 \
        || bad "$f is missing from the bundle or differs from this working tree"
done
ok "the bundle carries this working tree"

say "installing"
osascript -e "tell application \"${APP%.app}\" to quit" 2>/dev/null
sleep 2
pkill -f "$APP/Contents/MacOS" 2>/dev/null
# Whatever is on the port, not only our app: a leftover dev server holding 8765 lets
# the app start and quietly fail to serve anything.
lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null | xargs -r kill 2>/dev/null
sleep 1
[ -z "$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null)" ] || bad "something still holds port 8765"

rm -rf "$INSTALLED"
cp -R "$BUILT" /Applications/ || bad "could not copy into /Applications"
codesign --verify --deep --strict "$INSTALLED" 2>/dev/null \
    && ok "installed, and still verifies" \
    || bad "the installed copy does not verify"

# The stale entries, cleared the way Apple provides for it. Every build changes the
# code hash, so macOS stops recognising the grants and the rows in System Settings go
# on looking exactly like working ones — which is its own trap: the app is refused
# while appearing to be allowed. Resetting makes it ask again instead.
#
# Only removal can be scripted. Granting is a human decision by design, and rightly:
# anything able to hand itself the microphone without being asked would be malware.
say "permissions"
BUNDLE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$INSTALLED/Contents/Info.plist" 2>/dev/null)
if [ -n "$BUNDLE" ]; then
    # AudioCapture is the computer's own audio, and the only one of the three this
    # app still asks for besides the microphone. ScreenCapture stays in the list so
    # that a machine carrying the old grant from the ScreenCaptureKit days has it
    # taken away rather than left sitting there meaning nothing.
    for service in AudioCapture ScreenCapture Microphone; do
        tccutil reset "$service" "$BUNDLE" >/dev/null 2>&1 \
            && ok "$service cleared, it will ask again" \
            || printf '  \033[33m—\033[0m could not clear %s; remove it by hand in System Settings\n' "$service"
    done
else
    printf '  \033[33m—\033[0m no bundle identifier, so permissions were left alone\n'
fi

say "starting"
open -a "$INSTALLED"
n=0
[ -t 1 ] && printf '  waiting for the backend'
until curl -s -m 2 http://127.0.0.1:8765/api/state >/dev/null 2>&1 || [ "$n" -ge 60 ]; do
    [ -t 1 ] && printf '.'
    sleep 2; n=$((n + 2))
done
[ -t 1 ] && printf '\r\033[K'
[ "$n" -lt 60 ] || bad "the app started but its backend never answered"

# It has to be OUR app answering. It was a stale server the last time this looked fine.
pid=$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null | head -1)
root=$(ps -o ppid= -p "$(ps -o ppid= -p "$pid" | tr -d ' ')" | tr -d ' ')
case "$(ps -o command= -p "$root" 2>/dev/null)" in
    *"$APP"*) ok "answering on 8765, and it is the app" ;;
    *) bad "something other than the app is serving 8765" ;;
esac

# Running it used to drop a __pycache__ into its own Resources and break the seal.
[ -d "$INSTALLED/Contents/Resources/backend/__pycache__" ] \
    && bad "the app wrote bytecode into its bundle and broke its signature" \
    || ok "no bytecode written, signature survives running"

cat <<DONE

  The permissions were cleared, so the app will ask for them again the first time
  you record: the microphone for your voice, and system audio for the computer's
  side. Allow both. The system-audio one appears under
  Privacy & Security -> "System Audio Recording Only".

  bash mac/state.sh   what this machine is configured to do
DONE
