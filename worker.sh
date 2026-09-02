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

JOBS_DIR=${JOBS_DIR:-jobs}
MEDIA_DIR=${MEDIA_DIR:-media}
OUTPUT_DIR=${OUTPUT_DIR:-output}
RUN_SH=${RUN_SH:-./run.sh}
POLL_SECONDS=${POLL_SECONDS:-2}
PY=${PY:-.venv/bin/python}

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
    index, name, path, error = record.split("\t")
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

process_job() {
  local job="$1"
  local id result media_raw
  id=$(basename "$job" .job.json)
  result="$JOBS_DIR/$id.result.json"

  if ! media_raw=$(_read_media "$job" 2>/dev/null); then
    _emit_result "$result" "$(printf '1\tunknown\t\tmalformed job file')"
    rm -f "$job"
    return
  fi

  local media=() records=()
  [ -n "$media_raw" ] && mapfile -t media <<<"$media_raw"

  local i=0 rel name stem txt log
  for rel in "${media[@]}"; do
    i=$((i + 1))
    name=$(basename "$rel")
    stem="${name%.*}"
    txt="$OUTPUT_DIR/$stem/$stem.txt"
    log="$JOBS_DIR/run.$id.$i.log"
    echo "==> [$i] $rel"
    # Keep run.sh's own output; on failure it is the only clue we have.
    if "$RUN_SH" "$MEDIA_DIR/$rel" 2>&1 | tee "$log" && [ -f "$txt" ]; then
      rm -f "$log"
      records+=("$(printf '%s\t%s\t%s\t' "$i" "$name" "$stem/$stem.txt")")
    else
      records+=("$(printf '%s\t%s\t\t%s' "$i" "$name" "transcription failed (see $log)")")
    fi
  done

  _emit_result "$result" "${records[@]}"
  rm -f "$job"
}

main_loop() {
  echo "worker: polling $JOBS_DIR every ${POLL_SECONDS}s"
  while true; do
    local job
    job=$(find "$JOBS_DIR" -maxdepth 1 -name '*.job.json' 2>/dev/null | sort | head -n1)
    if [ -n "$job" ]; then
      process_job "$job"
    else
      sleep "$POLL_SECONDS"
    fi
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main_loop
fi
