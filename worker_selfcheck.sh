#!/usr/bin/env bash
# Self-check for worker.sh: feed it a good job and a malformed one, assert the
# result.json shape. run.sh is replaced by a stub.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/jobs" "$tmp/media" "$tmp/output"
: >"$tmp/media/5_77_abc123def456.mp4"

cat >"$tmp/fake-run.sh" <<'SH'
#!/usr/bin/env bash
name=$(basename "$1"); stem="${name%.*}"
mkdir -p "$OUTPUT_DIR/$stem"
printf '[SPEAKER_00]: hello\n' >"$OUTPUT_DIR/$stem/$stem.txt"
SH
chmod +x "$tmp/fake-run.sh"

export JOBS_DIR="$tmp/jobs" MEDIA_DIR="$tmp/media" OUTPUT_DIR="$tmp/output"
export RUN_SH="$tmp/fake-run.sh" PY="${PY:-.venv/bin/python}"

printf '{"media": ["5_77_abc123def456.mp4"]}' >"$tmp/jobs/20260101T000000-aaaaaaaa.job.json"
printf 'not json'                            >"$tmp/jobs/20260101T000001-bbbbbbbb.job.json"
# No weights here, so the llm branch is checked on its failure path: the point
# is that a broken job still produces a result file instead of stalling the bot.
printf '{"model": "/nope", "system": "s", "text": "t"}' \
                                             >"$tmp/jobs/20260101T000004-eeeeeeee.llm.json"

# shellcheck source=worker.sh
source ./worker.sh
for job in "$tmp"/jobs/*.job.json; do process_job "$job"; done
for job in "$tmp"/jobs/*.llm.json; do process_llm_job "$job"; done

"$PY" - "$tmp/jobs" <<'PY'
import glob, json, os, sys

jobs_dir = sys.argv[1]
results = sorted(glob.glob(os.path.join(jobs_dir, "*.result.json")))
assert len(results) == 3, results

good = json.load(open(results[0]))["results"]
assert good == [{"index": 1, "name": "5_77_abc123def456.mp4",
                 "transcript_path": "5_77_abc123def456/5_77_abc123def456.txt", "error": None}], good

bad = json.load(open(results[1]))["results"]
assert bad[0]["transcript_path"] is None and bad[0]["error"], bad

llm = json.load(open(results[2]))
assert "summary" not in llm and llm["error"], llm

leftover = [
    p for p in glob.glob(os.path.join(jobs_dir, "*.json"))
    if not p.endswith(".result.json")
]
assert not leftover, f"job files not deleted: {leftover}"
print("worker self-check OK")
PY
