#!/usr/bin/env bash
# Host-side spool runner for the Telegram transcription bot.
#
# Polls jobs/ for *.job.json written by the bot container, runs ./run.sh once
# per media item, writes jobs/<id>.result.json, deletes the job file. One job at
# a time. Started by launchd on login (see bot/deploy/).
#
# The functions are sourced by worker_selfcheck.sh; the poll loop only runs when
# this file is executed directly.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# launchd starts this with no shell environment, so the token lives in the same
# .env run.sh reads: mlx-lm's model downloads are rate-limited without it.
if [ -s .env ]; then
  set -a
  . ./.env
  set +a
fi

JOBS_DIR=${JOBS_DIR:-jobs}
MEDIA_DIR=${MEDIA_DIR:-media}
INPUT_DIR=${INPUT_DIR:-input}
OUTPUT_DIR=${OUTPUT_DIR:-output}
RUN_SH=${RUN_SH:-./run.sh}
CACHE_GC=${CACHE_GC:-./cache_gc.sh}
CACHE_MAX_GB=${CACHE_MAX_GB:-100}
POLL_SECONDS=${POLL_SECONDS:-2}
PY=${PY:-.venv/bin/python}
YTDLP=${YTDLP:-.venv/bin/yt-dlp}
# Chrome's cookie DB needs a Keychain key the headless worker can't get.
YOUTUBE_BROWSER=${YOUTUBE_BROWSER:-firefox}
LLM_MAX_TOKENS=${LLM_MAX_TOKENS:-8192}
TRANSCRIPT_CACHE="$OUTPUT_DIR/transcript-cache.json"
# mlx-lm downloads LLM_MODEL on first use and takes no cache argument, so the
# weights land next to the ASR models through the hub's own env var.
export HF_HUB_CACHE=${HF_HUB_CACHE:-$PWD/models}

_read_media() {
  # Print one media path per line; exit non-zero if the job file is malformed.
  "$PY" - "$1" <<'PY'
import json, sys
try:
    media = json.load(open(sys.argv[1]))["media"]
    assert isinstance(media, list) and all(isinstance(m, str) for m in media)
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
print("\n".join(media))
PY
}

_emit_result() {
  # _emit_result <result_file> <tab-separated: index,name,path,error> ...
  "$PY" - "$@" <<'PY'
import json, os, sys
result_file = sys.argv[1]
results = []
for record in sys.argv[2:]:
    index, name, path, error = record.split("\t", 3)
    results.append({
        "index": int(index),
        "name": name,
        "transcript_path": path or None,
        "error": error or None,
    })
tmp = result_file + ".tmp"
with open(tmp, "w") as fh:
    json.dump({"results": results}, fh)
os.replace(tmp, result_file)
PY
}

migrate_cache_name() {
  # The bot container and this worker restart independently, so the one-time
  # url-cache.json -> transcript-cache.json rename needs an owner that runs
  # before either side reads the file.
  local old="$OUTPUT_DIR/url-cache.json"
  [ -f "$old" ] && [ ! -f "$TRANSCRIPT_CACHE" ] && mv "$old" "$TRANSCRIPT_CACHE"
  return 0
}

_cache_transcript() {
  # Record <key> -> <transcript rel path> in output/transcript-cache.json so
  # the bot can skip the worker next time. <key> is a URL or a "<sha1><ext>"
  # media name (== the bot's MediaItem.filename).
  "$PY" - "$TRANSCRIPT_CACHE" "$1" "$2" <<'PY'
import json, os, sys
path, key, rel = sys.argv[1:4]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {}
data[key] = rel
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=1, sort_keys=True)
os.replace(tmp, path)
PY
}

process_llm_job() {
  # Stage 2 runs here rather than in the bot container: MLX needs the Mac GPU,
  # which Docker on macOS does not pass through. The weights are loaded per job
  # (~10s) instead of by a resident server holding ~9GB between batches.
  local job="$1" id result
  id=$(basename "$job" .llm.json)
  result="$JOBS_DIR/$id.result.json"
  echo "==> llm $id"

  "$PY" - "$job" "$result" "$LLM_MAX_TOKENS" <<'PY'
import json, os, sys

job_file, result_file, max_tokens = sys.argv[1:4]
try:
    from mlx_lm import generate, load

    job = json.load(open(job_file))
    model, tokenizer = load(job["model"])
    prompt = tokenizer.apply_chat_template(
        [
            {"role": "system", "content": job["system"]},
            {"role": "user", "content": job["text"]},
        ],
        add_generation_prompt=True,
        # Qwen and friends open <think> in the prompt itself; with thinking off
        # the model answers straight away.
        enable_thinking=job.get("thinking", False),
    )
    payload = {"summary": generate(model, tokenizer, prompt, max_tokens=int(max_tokens))}
except Exception as exc:
    payload = {"error": f"{type(exc).__name__}: {exc}"}
tmp = result_file + ".tmp"
with open(tmp, "w") as fh:
    json.dump(payload, fh)
os.replace(tmp, result_file)
PY
  rm -f "$job"
}

_fail_reason() {
  # run.sh exit code + its last output line: the Telegram reply carries the
  # whole story, since the host log file isn't reachable from the chat.
  local rc="$1" log="$2" last=""
  [ -s "$log" ] && last=$(tail -n1 "$log")
  echo "run.sh exited $rc${last:+: $last}"
}

run_cache_gc() {
  # FIFO-trim media/ + input/ to CACHE_MAX_GB. The one serialized actor that
  # touches these trees, so it runs here, before work. Files named in the job
  # about to run are still on disk in JOBS_DIR, so cache_gc keeps them.
  MEDIA_DIR="$MEDIA_DIR" INPUT_DIR="$INPUT_DIR" OUTPUT_DIR="$OUTPUT_DIR" \
    JOBS_DIR="$JOBS_DIR" CACHE_MAX_GB="$CACHE_MAX_GB" PY="$PY" \
    "$CACHE_GC" || echo "cache_gc failed; continuing" >&2
}

process_job() {
  local job="$1"
  local id result media_raw
  id=$(basename "$job" .job.json)
  result="$JOBS_DIR/$id.result.json"

  run_cache_gc

  if ! media_raw=$(_read_media "$job" 2>/dev/null); then
    _emit_result "$result" "$(printf '1\tunknown\t\tmalformed job file')"
    rm -f "$job"
    return
  fi

  # launchd runs this under /bin/bash, which is 3.2 on macOS: no mapfile, and
  # an empty array expands to an unbound variable under set -u.
  local media=() records=() line
  if [ -n "$media_raw" ]; then
    while IFS= read -r line; do media+=("$line"); done <<<"$media_raw"
  fi

  # Download the whole batch first, in one foreground run.sh pass: yt-dlp's own
  # sleep flags space the requests, which a link-at-a-time loop cannot do. The
  # per-item runs below then find the files already on disk.
  local urls=() rel
  for rel in ${media[@]+"${media[@]}"}; do
    case "$rel" in http://*|https://*) urls+=("$rel");; esac
  done
  if [ "${#urls[@]}" -gt 1 ]; then
    # A dead link aborts the pass; the per-item runs still report it per link,
    # so an incomplete prefetch is not a job failure.
    DOWNLOAD_ONLY=1 "$RUN_SH" "${urls[@]}" || echo "prefetch incomplete; continuing" >&2
  fi

  local i=0 rc name stem txt rel_txt log
  for rel in ${media[@]+"${media[@]}"}; do
    i=$((i + 1))
    log="$JOBS_DIR/run.$id.$i.log"
    echo "==> [$i] $rel"

    # A URL is handed to run.sh as-is (it runs yt-dlp); the transcript path
    # comes back on run.sh's TRANSCRIPT: line since the stem is the video title.
    if [[ "$rel" == http://* || "$rel" == https://* ]]; then
      name="$rel" rel_txt=""
      if "$RUN_SH" "$rel" 2>&1 | tee "$log"; then rc=0; else rc=$?; fi
      if [ "$rc" -eq 0 ]; then
        txt=$(sed -n 's/^TRANSCRIPT: //p' "$log" | tail -n1)
        if [ -n "$txt" ] && [ -f "$txt" ]; then
          name=$(basename "${txt%.txt}")
          rel_txt="${txt#"$OUTPUT_DIR"/}"
        fi
      fi
      if [ -n "$rel_txt" ]; then
        rm -f "$log"
        _cache_transcript "$rel" "$rel_txt"
        records+=("$(printf '%s\t%s\t%s\t' "$i" "$name" "$rel_txt")")
      else
        records+=("$(printf '%s\t%s\t\t%s' "$i" "$name" "$(_fail_reason "$rc" "$log")")")
      fi
      continue
    fi

    name=$(basename "$rel")
    stem="${name%.*}"
    txt="$OUTPUT_DIR/$stem/$stem.txt"
    if "$RUN_SH" "$MEDIA_DIR/$rel" 2>&1 | tee "$log"; then rc=0; else rc=$?; fi
    if [ "$rc" -eq 0 ] && [ -f "$txt" ]; then
      rm -f "$log"
      # A media name is its own content hash, so cache it too: a re-forward is
      # then free even after the blob is evicted.
      _cache_transcript "$name" "$stem/$stem.txt"
      records+=("$(printf '%s\t%s\t%s\t' "$i" "$name" "$stem/$stem.txt")")
    else
      records+=("$(printf '%s\t%s\t\t%s' "$i" "$name" "$(_fail_reason "$rc" "$log")")")
    fi
  done

  _emit_result "$result" ${records[@]+"${records[@]}"}
  rm -f "$job"
}

_emit_expand() {
  # _emit_expand <result_file> <raw "id<TAB>title" lines> <error>
  "$PY" - "$@" <<'PY'
import json, os, sys
result_file, raw, error = sys.argv[1], sys.argv[2], sys.argv[3]
videos = []
for line in raw.splitlines():
    if not line.strip():
        continue
    vid, _, title = line.partition("\t")
    videos.append({"id": vid, "title": title})
payload = {"videos": videos, "error": None if videos else (error or "no videos found")}
tmp = result_file + ".tmp"
with open(tmp, "w") as fh:
    json.dump(payload, fh)
os.replace(tmp, result_file)
PY
}

process_expand_job() {
  # yt-dlp and browser cookies live on the host, not in the bot container, so
  # playlist enumeration is a spool job. --flat-playlist is one API call
  # regardless of length; the bot slices to PLAYLIST_MAX and keeps the count.
  local job="$1" id result url raw err errf
  id=$(basename "$job" .expand.json)
  result="$JOBS_DIR/$id.result.json"
  echo "==> expand $id"
  url=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$job" 2>/dev/null)
  if [ -z "$url" ]; then
    _emit_expand "$result" "" "malformed expand job"
    rm -f "$job"
    return
  fi
  errf="$JOBS_DIR/expand.$id.err"
  raw=$("$YTDLP" --remote-components ejs:github --flat-playlist --simulate \
    --no-warnings --cookies-from-browser "$YOUTUBE_BROWSER" \
    --print $'%(id)s\t%(title)s' "$url" 2>"$errf") || true
  err=""
  [ -z "$raw" ] && err=$(tail -n1 "$errf" 2>/dev/null)
  rm -f "$errf"
  _emit_expand "$result" "$raw" "$err"
  rm -f "$job"
}

main_loop() {
  echo "worker: polling $JOBS_DIR every ${POLL_SECONDS}s"
  while true; do
    local job
    job=$(find "$JOBS_DIR" -maxdepth 1 \
      \( -name '*.job.json' -o -name '*.llm.json' -o -name '*.expand.json' \) \
      2>/dev/null | sort | head -n1)
    if [ -n "$job" ]; then
      case "$job" in
        *.llm.json) process_llm_job "$job" ;;
        *.expand.json) process_expand_job "$job" ;;
        *) process_job "$job" ;;
      esac
    else
      sleep "$POLL_SECONDS"
    fi
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  migrate_cache_name
  main_loop
fi
