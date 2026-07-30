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

say "checks first"
uv run --script test_app.py >/dev/null 2>&1 \
    && ok "the suite passes" \
    || bad "the suite fails — run: uv run --script test_app.py"
for f in web/*.js; do
    node --check "$f" >/dev/null 2>&1 || bad "$f does not parse"
done
ok "the frontend parses"

say "building"
(cd desktop && npm run build) >/tmp/lwt-build.log 2>&1 \
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

say "starting"
open -a "$INSTALLED"
n=0
until curl -s -m 2 http://127.0.0.1:8765/api/state >/dev/null 2>&1 || [ "$n" -ge 60 ]; do
    sleep 2; n=$((n + 2))
done
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

  The code hash changes with every build, so macOS treats this as a new app:
  remove the old entries with − in System Settings → Privacy & Security →
  Screen & System Audio Recording, and let it ask again on your first recording.

  bash mac/state.sh   what this machine is configured to do
DONE
