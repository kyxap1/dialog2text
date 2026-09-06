#!/usr/bin/env bash
# Self-check for worker.sh: feed it jobs of each kind, assert the result.json
# shapes. run.sh and yt-dlp are replaced by stubs. Also exercises cache_gc.sh.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/jobs" "$tmp/media" "$tmp/input" "$tmp/output"
: >"$tmp/media/5_77_abc123def456.mp4"

cat >"$tmp/fake-run.sh" <<'SH'
#!/usr/bin/env bash
if [ -n "${DOWNLOAD_ONLY:-}" ]; then echo "$*" >>"$OUTPUT_DIR/prefetch.log"; exit 0; fi
case "$1" in *FAIL*) echo "boom" >&2; exit 3;; esac
name=$(basename "$1"); stem="${name%.*}"
mkdir -p "$OUTPUT_DIR/$stem"
printf '[SPEAKER_00]: hello\n' >"$OUTPUT_DIR/$stem/$stem.txt"
echo "TRANSCRIPT: $OUTPUT_DIR/$stem/$stem.txt"
SH
chmod +x "$tmp/fake-run.sh"

cat >"$tmp/fake-ytdlp.sh" <<'SH'
#!/usr/bin/env bash
# Ignores every flag; prints two entries in the --print "%(id)s\t%(title)s" shape.
printf 'aaa\tFirst video\nbbb\tSecond video\n'
SH
chmod +x "$tmp/fake-ytdlp.sh"

export JOBS_DIR="$tmp/jobs" MEDIA_DIR="$tmp/media" INPUT_DIR="$tmp/input"
export OUTPUT_DIR="$tmp/output"
export RUN_SH="$tmp/fake-run.sh" YTDLP="$tmp/fake-ytdlp.sh" PY="${PY:-.venv/bin/python}"

printf '{"media": ["5_77_abc123def456.mp4"]}' >"$tmp/jobs/20260101T000000-aaaaaaaa.job.json"
printf 'not json'                            >"$tmp/jobs/20260101T000001-bbbbbbbb.job.json"
printf '{"media": ["https://youtu.be/xY9"]}' >"$tmp/jobs/20260101T000002-cccccccc.job.json"
printf '{"media": ["FAIL.mp4"]}'             >"$tmp/jobs/20260101T000003-dddddddd.job.json"
# No weights here, so the llm branch is checked on its failure path: the point
# is that a broken job still produces a result file instead of stalling the bot.
printf '{"model": "/nope", "system": "s", "text": "t"}' \
                                             >"$tmp/jobs/20260101T000004-eeeeeeee.llm.json"
printf '{"url": "https://www.youtube.com/playlist?list=PLtest"}' \
                                             >"$tmp/jobs/20260101T000005-ffffffff.expand.json"
printf '{"media": ["https://youtu.be/aa1", "https://youtu.be/bb2"]}' \
                                             >"$tmp/jobs/20260101T000006-99999999.job.json"

# shellcheck source=worker.sh
source ./worker.sh
for job in "$tmp"/jobs/*.job.json; do process_job "$job"; done
for job in "$tmp"/jobs/*.llm.json; do process_llm_job "$job"; done
for job in "$tmp"/jobs/*.expand.json; do process_expand_job "$job"; done

"$PY" - "$tmp/jobs" <<'PY'
import glob, json, os, sys

jobs_dir = sys.argv[1]
results = sorted(glob.glob(os.path.join(jobs_dir, "*.result.json")))
assert len(results) == 7, results

good = json.load(open(results[0]))["results"]
assert good == [{"index": 1, "name": "5_77_abc123def456.mp4",
                 "transcript_path": "5_77_abc123def456/5_77_abc123def456.txt", "error": None}], good

bad = json.load(open(results[1]))["results"]
assert bad[0]["transcript_path"] is None and bad[0]["error"], bad

url = json.load(open(results[2]))["results"]
assert url == [{"index": 1, "name": "xY9",
                "transcript_path": "xY9/xY9.txt", "error": None}], url

fail = json.load(open(results[3]))["results"]
assert fail[0]["transcript_path"] is None, fail
assert fail[0]["error"] == "run.sh exited 3: boom", fail

llm = json.load(open(results[4]))
assert "summary" not in llm and llm["error"], llm

batch = json.load(open(results[6]))["results"]
assert [r["name"] for r in batch] == ["aa1", "bb2"], batch
assert all(r["error"] is None for r in batch), batch
# One prefetch call carrying both links, not one call per link.
prefetch = open(os.path.join(jobs_dir, "..", "output", "prefetch.log")).read().splitlines()
assert prefetch == ["https://youtu.be/aa1 https://youtu.be/bb2"], prefetch

exp = json.load(open(results[5]))
assert exp["error"] is None, exp
assert [v["id"] for v in exp["videos"]] == ["aaa", "bbb"], exp
assert exp["videos"][0]["title"] == "First video", exp

cache = json.load(open(os.path.join(os.path.dirname(jobs_dir), "output", "transcript-cache.json")))
assert cache == {
    "https://youtu.be/xY9": "xY9/xY9.txt",
    "https://youtu.be/aa1": "aa1/aa1.txt",
    "https://youtu.be/bb2": "bb2/bb2.txt",
    "5_77_abc123def456.mp4": "5_77_abc123def456/5_77_abc123def456.txt",
}, cache

leftover = [
    p for p in glob.glob(os.path.join(jobs_dir, "*.json"))
    if not p.endswith(".result.json")
]
assert not leftover, f"job files not deleted: {leftover}"
print("worker self-check OK")
PY

# --- cache_gc.sh: FIFO trim to budget, keep-set honoured, output/ untouched ---
gc=$(mktemp -d)
trap 'rm -rf "$tmp" "$gc"' EXIT
mkdir -p "$gc/media" "$gc/input" "$gc/output" "$gc/jobs"
for i in 1 2 3 4 5; do
  f="$gc/media/f$i.mp3"; head -c 100 /dev/zero >"$f"
  touch -t "20260101000${i}.00" "$f"
done
# Oldest of all, but named in a job -> must survive.
head -c 100 /dev/zero >"$gc/input/keep_me.mp3"; touch -t 202601010000.00 "$gc/input/keep_me.mp3"
head -c 100 /dev/zero >"$gc/output/transcript.txt"
printf '{"media": ["keep_me.mp3"]}' >"$gc/jobs/20260101T000000-aaaaaaaa.job.json"
# 600 B total (kept files count too), budget 350 -> evict f1,f2,f3, stop at 300.
CACHE_MAX_BYTES=350 MEDIA_DIR="$gc/media" INPUT_DIR="$gc/input" \
  OUTPUT_DIR="$gc/output" JOBS_DIR="$gc/jobs" PY="$PY" ./cache_gc.sh

for f in f1 f2 f3; do
  [ ! -e "$gc/media/$f.mp3" ] || { echo "cache_gc: $f not evicted" >&2; exit 1; }
done
for f in f4 f5; do
  [ -e "$gc/media/$f.mp3" ] || { echo "cache_gc: $f wrongly evicted" >&2; exit 1; }
done
[ -e "$gc/input/keep_me.mp3" ] || { echo "cache_gc: kept file evicted" >&2; exit 1; }
[ -e "$gc/output/transcript.txt" ] || { echo "cache_gc: output/ touched" >&2; exit 1; }

# One byte over budget loses exactly one file, not all of them.
head -c 100 /dev/zero >"$gc/media/f6.mp3"; touch -t 202601010006.00 "$gc/media/f6.mp3"
# now f4,f5,f6 + kept keep_me = 400 B; budget 399 -> drop only f4.
CACHE_MAX_BYTES=399 MEDIA_DIR="$gc/media" INPUT_DIR="$gc/input" \
  OUTPUT_DIR="$gc/output" JOBS_DIR="$gc/jobs" PY="$PY" ./cache_gc.sh
[ ! -e "$gc/media/f4.mp3" ] || { echo "cache_gc: f4 not evicted" >&2; exit 1; }
[ -e "$gc/media/f5.mp3" ] && [ -e "$gc/media/f6.mp3" ] \
  || { echo "cache_gc: over-evicted past budget" >&2; exit 1; }

echo "cache_gc self-check OK"
