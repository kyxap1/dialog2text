# Telegram transcription bot — design

## Purpose

A personal Telegram bot, self-hosted on the author's laptop, that turns
forwarded video/audio messages into a single structured summary. It reuses the
existing `run.sh` pipeline (whisper-mlx transcription + pyannote diarization)
unchanged and adds an LLM post-processing pass driven by a fixed metaprompt
(spoken→written conversion, output format, structuring).

The summary is iterated on in place: after the first result, any text message is
a correction that re-runs the LLM pass over the already-transcribed batch.

The laptop is not always on. Telegram queues updates for up to 24h; the bot
catches up on reconnect after a sleep/wake. A full container restart drops the
in-memory session, so updates missed while it was down are lost — re-forward and
`/go` again (same tolerance as the batch not surviving a restart).

The bot runs under Docker Compose and connects to Telegram over MTProto
(Telethon). Transcription (`run.sh`) stays on the host; so does the LLM when the
local backend is used — see the next section.

The bot is a self-contained project in the `bot/` subdirectory: its own
`Dockerfile`, `docker-compose.yml`, Python dependencies and tests. It shares no
code with the whisper-mlx pipeline — the only contract between them is the
spool protocol below.

## Constraints and decisions

- **Private, single user.** Only the author's Telegram user ID is served;
  everything else is silently ignored.
- **Runs split across Docker and the host.** MLX — `whispermlx` /
  `parakeet-mlx`, and `vllm-metal` for the local LLM — needs Apple Metal, which
  Docker Desktop's Linux VM on macOS cannot reach. So **Stage 1 (transcription)
  stays on the host**, run by `run.sh` exactly as today, and the local LLM
  backend runs on the host too, as a Docker Model Runner service the bot reaches
  over HTTP. The bot runs under Docker Compose. Transcription and the bot talk
  through a shared `jobs/` spool directory on disk (protocol below).
- **Large files.** Forwarded videos exceed the 20 MB Bot API download limit, so
  the bot speaks MTProto via Telethon (bot-token auth) instead of the HTTP Bot
  API — a 2 GB download limit, and the bot chooses where each file lands on
  disk. It saves them under `media/<message-id>/`, bind-mounted to the host so
  the transcription worker reads the same tree. No `telegram-bot-api` server and
  no bot token in any file path.
- **MTProto, not webhook**: the laptop has no public endpoint and is
  intermittently online. Telethon reconnects itself across sleep/wake and
  restarts; while the laptop is off Telegram queues updates for ~24h.
- **No transcript chunking.** The combined transcript of a batch is fed to the
  LLM in one request. Context limits are accepted as out of scope for now; if a
  batch ever overflows, map-reduce joining is the upgrade path.
- **LLM provider is pluggable** — one `run_prompt` over an OpenAI-compatible
  endpoint, backend chosen by `LLM_PROVIDER`:
  - **Remote API** (`grok`, or any OpenAI-compatible host) — needs `LLM_API_KEY`.
    Grok (xAI, `api.x.ai`) is the reference; key and current pricing are
    confirmed at setup, not here.
  - **Local, on-demand** (`local`) — Docker Model Runner with the `vllm-metal`
    backend serves an MLX model on the host's Metal GPU over the same
    OpenAI-compatible API, no key; `LLM_BASE_URL` points at
    `host.docker.internal:12434/engines/v1`. DMR unloads the model after ~5 min
    idle (built-in, not tunable), so it only holds RAM around an actual `/go` or
    correction; the first request after idle pays a model-load delay.
- **Metaprompt lives in the repo** as a file, bind-mounted read-only into the
  bot container and loaded at runtime on every LLM pass. Its contents do not
  affect the design. It is edited in a text editor on the laptop, not over
  Telegram.
- **Two stages, only the cheap one repeats.** Transcription (minutes to hours)
  runs once per media item on the host; the LLM pass (seconds) re-runs on every
  correction inside the container. This needs no cache of its own — `run.sh`
  already writes transcripts to `output/<name>/<name>.txt`; the bot keeps the
  paths the worker reports.
- **A persistent reply keyboard**; no inline keyboards. The buttons just send
  commands as text, so every action is still a command or plain text. `/retry`
  is an alias for `/go` (transcription is idempotent — `/go` re-runs only the
  items that have no transcript yet).

## Components

1. **Bot process** — the only Docker Compose service, built from `bot/Dockerfile`
   with its own Python dependencies. Telethon over MTProto, bot-token auth,
   `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from my.telegram.org, in-memory
   session (re-auth on restart is instant). Responsibilities: whitelist check
   (`sender_id` match, no entity resolution), downloading each forwarded file to
   `media/<message-id>/`, batch state, command handlers (`/start`, `/go`
   (= `/retry`), `/queue`, `/reset`), text-message routing (prompt vs
   correction),
   job queue — plus writing Stage 1 job files into the spool and waiting for
   their result files. Mounts (bind, relative to the repo root —
   `bot/docker-compose.yml` uses `../`): `media` (downloaded files, shared with
   the worker), `jobs` (spool), `output` (read transcripts), `prompts`
   (metaprompt, read-only). `restart: unless-stopped`.
2. **Transcription worker** — host-side `worker.sh` at the repo root, run as a
   launchd LaunchAgent. Polling loop: pick the lexically-oldest
   `jobs/*.job.json`, for each media path run `./run.sh <file>`, read
   `output/<name>/<name>.txt`, write `jobs/<id>.result.json`, delete the job
   file. `run.sh` and the Python pipeline are unchanged. One job at a time.
3. **LLM client** — `run_prompt(system: str, text: str) -> str`, one
   OpenAI-compatible implementation for both backends; `LLM_PROVIDER` picks
   remote-API vs local. Runs inside the bot container; for `local` it calls the
   Docker Model Runner endpoint Compose injects. Mockable for tests.
4. **`prompts/default.md`** — the metaprompt, bind-mounted read-only into the
   bot container and read fresh on each LLM pass, so editing it on the host
   takes effect on the next correction without a restart.
5. **launchd LaunchAgent** — starts `worker.sh` on login. The bot itself is now
   started by Docker Compose; enable "Start Docker Desktop on login" so the
   stack comes up after a reboot.
6. **Docker Model Runner** (only when `LLM_PROVIDER=local`) — a host service
   (Docker Desktop 4.62+) serving an MLX model on the Metal GPU over an
   OpenAI-compatible API at `host.docker.internal:12434/engines/v1`. One-time
   host setup: `docker model install-runner --backend vllm`, then
   `docker model pull hf.co/mlx-community/…`. The bot points `LLM_BASE_URL` /
   `LLM_MODEL` at it; DMR loads on first request and unloads ~5 min after the
   last. Unused with a remote API.

## Deployment layout

```
whisper-mlx/
  run.sh, parakeet_transcribe.py, .venv/   host transcription, unchanged
  .env                                     HF_TOKEN for run.sh (host)
  worker.sh                                host spool runner, under launchd
  jobs/                                    spool, shared: bot container ↔ host worker
  output/                                  run.sh transcripts, read by the bot container
  media/                                   bind mount: files the bot downloads, per message id
  prompts/default.md                       metaprompt, read-only into the bot container
  bot/
    Dockerfile
    docker-compose.yml                     bot only; mounts ../jobs, ../output, ../media, ../prompts
    pyproject.toml / requirements.txt      bot's own Python context
    .env                                   bot config for Compose
    src/…
    tests/
```

The bot shares the whisper-mlx working tree only for the four bind-mounted
directories; it imports no repo code. A separate repository is possible (the
contract is just the spool protocol) but adds a second clone and duplicated
`jobs/`/`output/` paths for no gain at this size.

Bot config, via `bot/.env` that Compose reads: `TELEGRAM_BOT_TOKEN`,
`ADMIN_USER_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `LLM_PROVIDER`,
`LLM_MODEL`, `LLM_BASE_URL`. A remote API also needs `LLM_API_KEY`; for
`LLM_PROVIDER=local` no key is needed and `LLM_BASE_URL` points at the DMR
endpoint on the host.

### Spool protocol

Filesystem only, no SSH, a single writer on each side.

- **Job** — bot writes `jobs/<id>.job.json`:
  `{ "media": ["<message-id>/file_123.mp4", ...] }`. Paths are relative to the
  `media/` root, so they resolve on both the bot container and the host.
- **Claim** — the worker takes the lexically-oldest `*.job.json`. No lock file:
  there is one worker and it processes one job at a time.
- **Result** — worker writes `jobs/<id>.result.json`:
  `{ "results": [ { "index": 1, "name": "...",
  "transcript_path": "<name>/<name>.txt" | null, "error": "..." | null }, ... ] }`.
  Paths are relative to the `output/` root. Then it deletes the job file.
- **Pickup** — the bot polls for `jobs/<id>.result.json`, resolves each
  `transcript_path` against its `./output` mount, and proceeds to Stage 2.
- **Crash recovery** — if the worker dies mid-job the `.job.json` is still there
  and is reprocessed on restart. `run.sh` skips media whose transcript already
  exists, so a replay re-transcribes nothing — the same idempotency the batch
  model already relies on.

## Data flow

```
forward video/audio       → bot downloads it to media/<message-id>/ → path appended to batch.media
text, no output yet       → appended to batch.extra_prompt
/go                       → write jobs/<id>.job.json; await jobs/<id>.result.json; then Stage 2. Batch is KEPT.
text, output exists        → appended to batch.corrections, then Stage 2 re-runs (in-container, no spool)
/reset                    → clear the batch
```

**Stage 1 — transcription (once per item).** On `/go` the bot writes one job
file listing every batch media item that has no transcript yet, then waits for
the result file. The host worker runs `run.sh <file>` per item in forward
order; on failure it records which item failed (`error` set, `transcript_path`
null) and continues with the rest. Items already carrying a transcript path are
not listed in the job, so a re-run never re-transcribes. The bot merges the
result back into `batch.media` and reports the index→filename mapping.

**Stage 2 — LLM pass (repeats).** Runs inside the bot container. Concatenate
the transcripts, each under a heading carrying its index and source name:

```
=== [1] Совещание 12 марта ===
[SPEAKER_00]: ...

=== [2] Интервью с подрядчиком ===
[SPEAKER_01]: ...
```

The headings are what make per-item corrections work: "fix B in the second one"
is ordinary prompt text, and the LLM maps it via the index. The bot reports the
index→filename mapping when stage 1 finishes.

Then:

```
summary = run_prompt(
    system = default.md + extra_prompt + corrections (all of them, in order),
    text   = concatenated transcripts,
)
```

The result is sent as a `.md` document plus a short text preview.

### Batch state

There is one batch (single user), held in memory:

- `media`: list of `(index, filename, transcript_path | None)`, in forward order
- `extra_prompt`: text messages received before the first `/go`
- `corrections`: text messages received after output exists, in order
- `has_output`: whether stage 2 has produced a result — this is what makes a
  text message a correction rather than a prompt addition

Corrections accumulate; every re-run applies all of them, so "fix A" and
"fix B" compose instead of overwriting each other.

Lost on bot-container restart. The transcripts themselves survive in `output/`,
so the cost is re-forwarding, not re-transcribing.

### Queue

The bot keeps its single in-process `asyncio` queue for serializing `/go` jobs
and Stage 2 re-runs. Stage 1 is executed by the host worker, which is itself
strictly sequential, so there is still exactly one transcription running at a
time — the transcription pipeline already caps itself at ~75% of free RAM/CPU.
On `/go` the bot replies with queue position; it posts progress as each media
item's result lands.

The queue also keeps Stage 1 and Stage 2 from overlapping, so transcription and
a local LLM pass never run at the same moment. A just-used local model can still
sit in host RAM during its ~5 min idle window while the next transcription runs;
the 75% cap leaves headroom for that.

## Error handling

| Situation | Behaviour |
|---|---|
| Message from non-whitelisted user | Ignored silently |
| Download fails (too big / network) | Error message to user; item not added to the batch |
| One media item fails transcription | Worker sets `error` and leaves `transcript_path` null; bot notes which item failed, continues with the rest, mentions it in the final message; a later `/go` retries just that item |
| All media items fail | Report failure; `/go` retries |
| Worker not running / Docker can't see the spool | Job file sits in `jobs/` untouched; the bot reports "queued" and keeps waiting. It runs when the worker is back — same tolerance as the laptop being asleep |
| Malformed job file | Worker writes a `result.json` with every item errored; bot reports the batch failed, `/go` retries |
| LLM error — remote API down, or the local model / DMR unavailable | Send the raw concatenated transcripts as a document + an error note. The batch is kept either way, so the correction that triggered it can simply be re-sent |
| Correction sent while a job is running | Queued behind it, applied on the next stage 2 |
| Bot crash mid-job | Job lost; polling replays queued Telegram updates on restart; user re-runs `/go`. Any in-flight `.job.json` is still picked up by the worker; its `result.json` is ignored by the restarted bot and reused on the next `/go` |

## Testing

- **Unit** (`bot/tests/`, run in the bot container; LLM client and the spool
  mocked):
  - batch accumulation — media order, `extra_prompt` before output vs
    `corrections` after
  - a correction re-runs stage 2 only, writes no job file, and stage 1 is not
    re-entered
  - corrections accumulate: after "fix A" then "fix B", the prompt carries both
  - Stage 1 writes a well-formed job file for exactly the untranscribed items
  - the bot merges a `result.json` (relative paths, per-item errors) back into
    `batch.media` correctly, including "one item fails, rest continue" and the
    failed item retried on `/go`
  - queue sequencing
- **Worker**: one shell self-check next to `worker.sh` — feed it a fixture
  `job.json` and a malformed one, assert the produced `result.json` shape.
  `run.sh` is mocked.
- **LLM client**: mocked in all bot tests; the provider gate covered by a unit
  test (`grok`/`local` accepted, unknown rejected). A thin live check per
  backend — a trivial prompt against the remote API, and `docker model` + a
  trivial prompt for local — run manually.
- **End-to-end**: manual — `cd bot && docker compose up`, start the worker,
  forward a real batch, `/go`, send a correction, inspect both returned `.md`
  files.

## Out of scope (for now)

Deliberately cut. Each names what would bring it back.

- **Persisting the batch across restarts** — expected to be the first thing that
  returns, since the laptop sleeps and wakes. Add it when a lost batch actually
  costs a re-forward that hurts.
- **Full Docker, CPU-only transcription** — replacing MLX with a Linux CPU
  Whisper build so Stage 1 also containerizes. Rejected: much slower, a second
  pipeline to maintain, and it drops the "reuse `run.sh` unchanged" property.
  Revisit only if the host worker becomes a real operational burden.
- **SSH bridge instead of the spool** — bot in-container calling
  `ssh host.docker.internal ./run.sh`. Rejected for now: needs sshd plus a key
  in the container for no real gain over a spool directory. The spool is the
  upgrade target if job metadata grows.
- **Inline keyboards** (`/go`, `/reset`, re-run buttons, reset confirmation) —
  add when typing the commands gets annoying.
- **Editing the metaprompt over Telegram** — it is a file in this repo on this
  laptop. Add only if editing it from a phone becomes a real need.
- **Prompting on "new video while a batch has output"** — currently the video
  just joins the batch; `/reset` first to start fresh. Add the "add to batch /
  start new" question if that silently produces a wrong batch in practice.
- **Promoting a correction into `default.md`** — corrections are per-batch by
  design.
- Transcript chunking / context-window management
- Per-video summaries (only the combined summary is produced)
- Returning raw per-video transcripts (only on a future flag)
- Any non-author user, rate limiting, multi-tenant concerns
- **Keeping the local model resident / a configurable idle timeout** — DMR's
  built-in ~5 min unload is taken as-is. Revisit only if the reload delay on the
  first `/go` after idle becomes annoying in practice
- **Auto-selecting the backend** (fall back to local when the API key is
  missing, or vice versa) — `LLM_PROVIDER` is set explicitly
- **A Compose `models:` block for DMR** — a one-time `docker model pull` plus
  two env vars is less machinery. Adopt the block if model management becomes a
  chore
