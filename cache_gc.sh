#!/usr/bin/env bash
# FIFO (oldest mtime first) trim of MEDIA_DIR + INPUT_DIR down to CACHE_MAX_GB.
#
# Files named in any JOBS_DIR/*.job.json are kept even if old. OUTPUT_DIR is
# never scanned: transcripts are kilobytes and are what makes a re-send free
# after the blob is gone. mtime is read, never written, so a cache hit does
# not reorder anything -- FIFO, not LRU.
#
# Soft cap: called between jobs, not between a job's own downloads, so one
# large playlist can overshoot by its own audio before the next run.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MEDIA_DIR=${MEDIA_DIR:-media}
INPUT_DIR=${INPUT_DIR:-input}
JOBS_DIR=${JOBS_DIR:-jobs}
PY=${PY:-.venv/bin/python}
CACHE_MAX_GB=${CACHE_MAX_GB:-100}
# Test hook: an exact byte budget, bypassing the GB->bytes conversion.
CACHE_MAX_BYTES=${CACHE_MAX_BYTES:-}

"$PY" - "$MEDIA_DIR" "$INPUT_DIR" "$JOBS_DIR" "$CACHE_MAX_GB" "$CACHE_MAX_BYTES" <<'PY'
import glob, json, os, sys

media_dir, input_dir, jobs_dir, max_gb, max_bytes = sys.argv[1:6]
budget = int(max_bytes) if max_bytes else int(float(max_gb) * 1024**3)

keep = set()
for jf in glob.glob(os.path.join(jobs_dir, "*.job.json")):
    try:
        for m in json.load(open(jf)).get("media", []):
            keep.add(os.path.basename(m))
    except (OSError, ValueError):
        pass

files = []
for d in (media_dir, input_dir):
    if not os.path.isdir(d):
        continue
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            files.append((p, name, os.path.getsize(p)))

total = sum(size for _, _, size in files)
evictable = sorted(
    (f for f in files if f[1] not in keep),
    key=lambda f: os.path.getmtime(f[0]),
)
for p, name, size in evictable:
    if total <= budget:
        break
    os.remove(p)
    total -= size
    print(f"cache_gc: evicted {name} ({size} B)", file=sys.stderr)
PY
