#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")"

# HF_TOKEN is picked up from the environment by huggingface_hub, so it never lands in the
# process arguments (where any other program could read it via `ps`).
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

LANGUAGE=${LANGUAGE:-ru}
SPEAKERS=${SPEAKERS:-2}
MODEL=${MODEL:-large-v3}
FORMAT=${FORMAT:-all}
OUTPUT_DIR=${OUTPUT_DIR:-output}

if [ "$#" -eq 0 ]; then
  shopt -s nullglob
  set -- input/*
fi

if [ "$#" -eq 0 ]; then
  echo "Nothing to do: the input/ folder is empty. Copy your video or audio files there."
  exit 1
fi

mkdir -p "$OUTPUT_DIR" models

for file in "$@"; do
  name=$(basename "$file")
  out="$OUTPUT_DIR/${name%.*}"
  mkdir -p "$out"
  echo "==> $name"
  .venv/bin/whispermlx "$file" \
    --model "$MODEL" --language "$LANGUAGE" \
    --model_dir models --output_dir "$out" --output_format "$FORMAT" \
    --diarize --min_speakers "$SPEAKERS" --max_speakers "$SPEAKERS"
done

echo "Done. Transcripts are in the $OUTPUT_DIR/ folder."
