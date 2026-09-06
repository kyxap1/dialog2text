#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")"

if ! command -v brew >/dev/null; then
  echo "Homebrew is not installed. Install it first, then run this again:"
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi

brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
# pyannote's torchcodec 0.7 links FFmpeg <=7; keg-only so it coexists with the
# current ffmpeg. run.sh points the loader at it.
brew list ffmpeg@7 >/dev/null 2>&1 || brew install ffmpeg@7
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12

[ -d .venv ] || "$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

mkdir -p input output models

if [ ! -s .env ]; then
  # The file, not the shell, is what a launchd-started worker sees; an already
  # exported HF_TOKEN (direnv, CI) just gets written out. umask: mode 600, the
  # token must never be world-readable.
  (umask 077 && printf 'HF_TOKEN=%s\n' "${HF_TOKEN:-}" > .env)
  if [ -n "${HF_TOKEN:-}" ]; then
    echo "Wrote .env with the HF_TOKEN from this environment — keep the file private."
  else
    echo
    echo "Put a Hugging Face token into .env — the speaker-detection model needs it:"
    echo "1) create a read token: https://huggingface.co/settings/tokens"
    echo "2) accept the model terms: https://huggingface.co/pyannote/speaker-diarization-community-1"
  fi
fi

echo
echo "Setup complete. Put your video/audio files into the input/ folder, then start run.command."
