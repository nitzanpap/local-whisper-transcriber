#!/bin/bash
# Everything this needs, checked and offered, in one command.
#
# Nothing is installed without saying what and waiting for a yes. Run it again any
# time: it only ever does the parts that are missing, so it doubles as a way of
# asking "what is not ready yet".

set -u
cd "$(dirname "$0")" || exit 1

MODELS="${MODELS:-$HOME/whisper-models}"
MODEL_URL=https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
VAD_URL=https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
miss() { printf '  \033[33m—\033[0m %s\n' "$1"; }
say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

ask() {  # ask "what it is" "the command that installs it"
    printf '\n  %s\n      %s\n  install it? [y/N] ' "$1" "$2"
    read -r reply
    case "$reply" in [yY]*) eval "$2" ;; *) echo "  skipped" ;; esac
}

say "what this needs"
NEED=0
for tool in uv ffmpeg ffprobe whisper-cli; do
    if command -v "$tool" >/dev/null; then ok "$tool"; else miss "$tool"; NEED=1; fi
done
[ -f "$MODELS/ggml-large-v3.bin" ] && ok "a whisper model" || { miss "a whisper model"; NEED=1; }
[ -f "$MODELS/ggml-silero-v5.1.2.bin" ] && ok "a VAD model (optional)" || miss "a VAD model (optional)"

if [ "$NEED" = "0" ]; then
    say "nothing missing"
else
    say "installing what is missing"
    command -v brew >/dev/null || {
        echo "  Homebrew is not installed, and everything below comes from it."
        echo "  https://brew.sh, then run this again."
        exit 1
    }
    command -v uv          >/dev/null || ask "uv runs the app and its dependencies" "brew install uv"
    command -v ffmpeg      >/dev/null || ask "ffmpeg reads and records audio"       "brew install ffmpeg"
    command -v whisper-cli >/dev/null || ask "whisper-cli does the transcribing"    "brew install whisper-cpp"
    # About 3 GB. Offered rather than assumed, because it is a long download on a
    # connection nobody asked about.
    [ -f "$MODELS/ggml-large-v3.bin" ] || ask \
        "the large-v3 model, about 3 GB — the smaller ones are faster and worse" \
        "mkdir -p '$MODELS' && curl -L --progress-bar -o '$MODELS/ggml-large-v3.bin' '$MODEL_URL'"
    [ -f "$MODELS/ggml-silero-v5.1.2.bin" ] || ask \
        "the VAD model, about 1 MB — skips silence so whisper invents less in it" \
        "mkdir -p '$MODELS' && curl -L --progress-bar -o '$MODELS/ggml-silero-v5.1.2.bin' '$VAD_URL'"
fi

say "checking it works"
uv run --script test_app.py >/dev/null 2>&1 \
    && ok "the checks pass" \
    || { printf '  \033[31m✗\033[0m the checks fail — run: uv run --script test_app.py\n'; exit 1; }

say "ready"
cat <<'DONE'
  uv run --script app.py        the app, at http://127.0.0.1:8765
  uv run --script test_app.py   the checks, about five seconds
  bash mac/state.sh             what your machine is configured to do
  bash mac/verify-capture.sh    recording, on real hardware

  The model goes in Settings the first time. Recording needs macOS to allow the
  microphone and the screen, and it will ask when you first press record.
DONE
