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

LANGUAGE=${LANGUAGE:-}
SPEAKERS=${SPEAKERS:-}
#MODEL=${MODEL:-large-v3} # whisper
MODEL=${MODEL:-parakeet-v3}
FORMAT=${FORMAT:-txt}
CHUNK_DURATION=${CHUNK_DURATION:-180}
OUTPUT_DIR=${OUTPUT_DIR:-output}
HF_TOKEN=${HF_TOKEN:?}
# Chrome's cookie DB needs a Keychain key the headless spool worker can't get.
YOUTUBE_BROWSER=${YOUTUBE_BROWSER:-firefox}
SHOW_RESULT=${SHOW_RESULT:-1}

# Take 75% of what's currently free (not 75% of the machine), so the rest of
# the laptop stays usable while this runs. Override CPU_THREADS/MEM_LIMIT_BYTES
# to skip the auto-calculation.
if [ -z "${CPU_THREADS:-}" ]; then
  ncpu=$(sysctl -n hw.ncpu)
  idle_pct=$(top -l 1 | awk '/CPU usage/{print $7}' | tr -d '%')
  CPU_THREADS=$(awk -v n="$ncpu" -v idle="${idle_pct:-100}" 'BEGIN{v=int(n*idle/100*0.75); if(v<1)v=1; print v}')
fi
if [ -z "${MEM_LIMIT_BYTES:-}" ]; then
  page_size=$(vm_stat | head -1 | grep -oE '[0-9]+')
  free_pages=$(vm_stat | awk '/Pages free/{gsub("[.]","",$3); print $3}')
  inactive_pages=$(vm_stat | awk '/Pages inactive/{gsub("[.]","",$3); print $3}')
  MEM_LIMIT_BYTES=$(awk -v p="$page_size" -v f="$free_pages" -v i="$inactive_pages" 'BEGIN{print int((f+i)*p*0.75)}')
fi
export OMP_NUM_THREADS="$CPU_THREADS" MKL_NUM_THREADS="$CPU_THREADS" \
  VECLIB_MAXIMUM_THREADS="$CPU_THREADS" NUMEXPR_NUM_THREADS="$CPU_THREADS" MEM_LIMIT_BYTES
echo "Using $CPU_THREADS CPU threads, $((MEM_LIMIT_BYTES / 1024 / 1024)) MB memory limit (75% of what was free)."
NICE="nice -n 10"

mkdir -p "$OUTPUT_DIR" models input
shopt -s nullglob

# Each argument is a URL, "*" (default: everything in input/), or file1,file2 --
# bare names are looked up inside input/, anything with a "/" is used as given.
args=("$@")
[ "${#args[@]}" -eq 0 ] && args=("*")

files=()
for arg in "${args[@]}"; do
  IFS=',' read -ra tokens <<< "$arg"
  for token in "${tokens[@]}"; do
    if [[ "$token" == http://* || "$token" == https://* ]]; then
      # Pull metadata only first. yt-dlp forces mp3 and names files <title>.mp3,
      # so we can spot an existing copy; re-download unless ffprobe confirms its
      # duration matches (a truncated file reads shorter).
      # A plain read loop, not mapfile: launchd starts the worker under macOS's
      # own bash 3.2, which has no mapfile.
      meta=()
      while IFS= read -r meta_line; do meta+=("$meta_line"); done \
        < <($NICE .venv/bin/yt-dlp --remote-components ejs:github \
          --simulate --no-warnings --print "%(filename)s" --print "%(duration)s" \
          --cookies-from-browser "$YOUTUBE_BROWSER" -o "input/%(title)s.%(ext)s" "$token")
      # yt-dlp writes errors to stderr and nothing to stdout on failure (bad
      # cookies, private video, network) -- turn that into a visible error.
      [ "${#meta[@]}" -ge 2 ] || { echo "yt-dlp returned no metadata for $token" >&2; exit 1; }
      file="${meta[0]%.*}.mp3"
      # No cached file yet is the normal case; don't let ffprobe's failure abort.
      have=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$file" 2>/dev/null || true)
      if [[ -s "$file" ]] && awk -v h="${have:-0}" -v w="${meta[1]:-0}" 'BEGIN{exit !(h >= w - 2)}'; then
        echo "==> cached $file"
      else
        echo "==> downloading $token"
        $NICE .venv/bin/yt-dlp --remote-components ejs:github -x --audio-format mp3 \
          --cookies-from-browser "$YOUTUBE_BROWSER" -o "input/%(title)s.%(ext)s" "$token"
      fi
      files+=("$file")
    elif [[ "$token" == "*" ]]; then
      files+=(input/*)
    elif [[ "$token" == */* ]]; then
      files+=("$token")
    else
      files+=("input/$token")
    fi
  done
done

if [ "${#files[@]}" -eq 0 ]; then
  echo "Nothing to do: the input/ folder is empty. Copy your video or audio files there, or pass a URL."
  exit 1
fi

# Empty LANGUAGE/SPEAKERS means auto-detect -- only pass the flags when set.
language_args=()
[ -n "$LANGUAGE" ] && language_args=(--language "$LANGUAGE")
speaker_args=()
[ -n "$SPEAKERS" ] && speaker_args=(--min_speakers "$SPEAKERS" --max_speakers "$SPEAKERS")

for file in "${files[@]}"; do
  name=$(basename "$file")
  stem="${name%.*}"
  out="$OUTPUT_DIR/$stem"
  txt="$out/$stem.txt"
  # A content-addressed media file (40-hex stem) already transcribed: the bytes
  # can't have changed, so reuse it. A URL's stem is the video title, not a
  # hash, so URLs still go through yt-dlp's duration check above.
  if [ "${#stem}" -eq 40 ] && [[ "$stem" =~ ^[0-9a-f]+$ ]] && [ -f "$txt" ]; then
    echo "==> cached transcript $txt"
    echo "TRANSCRIPT: $txt"
    if [ "$SHOW_RESULT" = "1" ]; then echo "--- $txt ---"; cat "$txt"; fi
    continue
  fi
  mkdir -p "$out"
  echo "==> $name"
  if [[ "$MODEL" == parakeet* ]]; then
    $NICE .venv/bin/python parakeet_transcribe.py "$file" \
      --hf_token "$HF_TOKEN" \
      --model "$MODEL" \
      --model_dir models --output_dir "$out" --output_format "$FORMAT" \
      --chunk_duration "$CHUNK_DURATION" \
      --diarize "${speaker_args[@]}"
  else
    $NICE .venv/bin/whispermlx "$file" \
      --hf_token "$HF_TOKEN" \
      --model "$MODEL" "${language_args[@]}" \
      --model_dir models --output_dir "$out" --output_format "$FORMAT" \
      --diarize "${speaker_args[@]}"
  fi

  # Machine-readable so the spool worker can find the transcript for a URL job,
  # where the output stem is the video title and only known after yt-dlp runs.
  echo "TRANSCRIPT: $txt"
  if [ "$SHOW_RESULT" = "1" ] && [ -f "$txt" ]; then
    echo "--- $txt ---"
    cat "$txt"
  fi
done

echo "Done. Transcripts are in the $OUTPUT_DIR/ folder."
