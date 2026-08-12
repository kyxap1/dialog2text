#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")"

if ! command -v brew >/dev/null; then
  echo "Homebrew is not installed. Install it first, then run this again:"
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi

brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12

[ -d .venv ] || "$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

mkdir -p input output models

if [ ! -f .env ]; then
  echo
  echo "Hugging Face token — needed once, to download the speaker-detection model."
  echo "1) create a read token: https://huggingface.co/settings/tokens"
  echo "2) accept the model terms: https://huggingface.co/pyannote/speaker-diarization-community-1"
  # hidden input + umask 077: the token must never appear on screen or be world-readable
  read -rsp "Paste the token here (nothing will show up) and press Enter: " token
  echo
  if [ -n "$token" ]; then
    umask 077
    printf 'HF_TOKEN=%s\n' "$token" > .env
    echo "Token saved to .env — keep this file private."
  else
    echo "Skipped. Add HF_TOKEN=... to a file named .env later if diarization fails."
  fi
fi

echo
echo "Setup complete. Put your video/audio files into the input/ folder, then start run.command."
