# Telegram transcription bot — design

## Purpose

A personal Telegram bot, self-hosted on the author's laptop, that turns
forwarded video/audio messages into a single structured summary. It reuses the
existing `run.sh` pipeline (whisper-mlx transcription + pyannote diarization)
unchanged and adds an LLM post-processing pass driven by a fixed metaprompt
(spoken→written conversion, output format, structuring).

The summary is iterated on in place: after the first result, any text message is
a correction that re-runs the LLM pass over the already-transcribed batch.

The laptop is not always on. When it is off, Telegram queues updates for up to
24h and the bot catches up on reconnect.

The bot, the local Telegram API server and the LLM client run under Docker
Compose. Transcription (`run.sh`) stays on the host — see the next section.

The bot is a self-contained project in the `bot/` subdirectory: its own
`Dockerfile`, `docker-compose.yml`, Python dependencies and tests. It shares no
code with the whisper-mlx pipeline — the only contract between them is the
spool protocol below.

## Constraints and decisions

- **Private, single user.** Only the author's Telegram user ID is served;
  everything else is silently ignored.
- **Runs split across Docker and the host.** MLX (`whispermlx` /
  `parakeet-mlx`) needs Apple Metal, which Docker Desktop's Linux VM on macOS
  cannot reach. So **Stage 1 (transcription) stays on the host**, run by
  `run.sh` exactly as today. Everything that *can* be containerized — the
  Telegram local API server, the bot, the LLM client — runs under Docker
  Compose. The two halves talk through a shared `jobs/` spool directory on
  disk (protocol below).
- **Large files.** Forwarded videos exceed the 20 MB Bot API download limit, so
  a local `telegram-bot-api` server runs alongside the bot as a Compose service
  (2 GB limit, and in local mode it writes the file to disk and hands back a
  path — no download step). Its data dir is bind-mounted to the host so the
  file it writes is reachable by the host transcription worker.
- **Long polling**, not webhook: the laptop has no public endpoint and is
  intermittently online. Polling reconnects itself across sleep/wake and
  restarts.
- **No transcript chunking.** The combined transcript of a batch is fed to the
  LLM in one request. Context limits are accepted as out of scope for now; if a
  batch ever overflows, map-reduce joining is the upgrade path.
- **LLM provider is pluggable.** Grok (xAI, OpenAI-compatible `api.x.ai`) is the
  default. A Claude implementation can be added later behind the same interface.
  Grok API key and current free-tier terms must be confirmed at setup — not
  verified here.
- **Metaprompt lives in the repo** as a file, bind-mounted read-only into the
  bot container and loaded at runtime on every LLM pass. Its contents do not
  affect the design. It is edited in a text editor on the laptop, not over
  Telegram.
- **Two stages, only the cheap one repeats.** Transcription (minutes to hours)
  runs once per media item on the host; the LLM pass (seconds) re-runs on every
  correction inside the container. This needs no cache of its own — `run.sh`
  already writes transcripts to `output/<name>/<name>.txt`; the bot keeps the
  paths the worker reports.
- **No inline keyboards.** Commands and plain text messages cover every action.

## Components

1. **`telegram-bot-api` local server** — Docker Compose service defined in
   `bot/docker-compose.yml`, image `aiogram/telegram-bot-api`, started in local
   mode (`TELEGRAM_LOCAL=true`).
   Needs `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from my.telegram.org. Its data
   dir (`/var/lib/telegram-bot-api`) is bind-mounted to host `./tg-data`, so a
   file it writes to disk in local mode is directly readable by the host
   transcription worker — no download step.
2. **Bot process** — Docker Compose service, built from `bot/Dockerfile` with
   its own Python dependencies. `python-telegram-bot`, long polling, base URL
   `http://telegram-bot-api:8081`. Responsibilities: whitelist check, batch
   state, command handlers (`/go`, `/reset`), text-message routing (prompt
   addition vs correction), job queue — plus writing Stage 1 job files into the
   spool and waiting for their result files. Mounts (bind, relative to the repo
   root — `bot/docker-compose.yml` uses `../`): `tg-data` (same path the API
   server uses), `jobs` (spool), `output` (read transcripts), `prompts`
   (metaprompt, read-only). `restart: unless-stopped`.
3. **Transcription worker** — host-side `worker.sh` at the repo root, run as a
   launchd LaunchAgent. Polling loop: pick the lexically-oldest
   `jobs/*.job.json`, for each media path run `./run.sh <file>`, read
   `output/<name>/<name>.txt`, write `jobs/<id>.result.json`, delete the job
   file. `run.sh` and the Python pipeline are unchanged. One job at a time.
4. **LLM client** — `run_prompt(system: str, text: str) -> str`. One Grok
   implementation now; provider chosen by env var. Runs inside the bot
   container. Mockable for tests.
5. **`prompts/default.md`** — the metaprompt, bind-mounted read-only into the
   bot container and read fresh on each LLM pass, so editing it on the host
   takes effect on the next correction without a restart.
6. **launchd LaunchAgent** — starts `worker.sh` on login. The bot itself is now
   started by Docker Compose; enable "Start Docker Desktop on login" so the
   stack comes up after a reboot.

## Deployment layout

```
whisper-mlx/
  run.sh, parakeet_transcribe.py, .venv/   host transcription, unchanged
  .env                                     HF_TOKEN for run.sh (host)
  worker.sh                                host spool runner, under launchd
  jobs/                                    spool, shared: bot container ↔ host worker
  output/                                  run.sh transcripts, read by the bot container
  tg-data/                                 bind mount: media the API server writes
  prompts/default.md                       metaprompt, read-only into the bot container
  bot/
    Dockerfile
    docker-compose.yml                     telegram-bot-api + bot; mounts ../jobs, ../output, ../tg-data, ../prompts
    pyproject.toml / requirements.txt      bot's own Python context
    .env                                   bot + API config for Compose
    src/…
    tests/
```

The bot shares the whisper-mlx working tree only for the four bind-mounted
directories; it imports no repo code. A separate repository is possible (the
contract is just the spool protocol) but adds a second clone and duplicated
`jobs/`/`output/` paths for no gain at this size.

Bot and API config, via `bot/.env` that Compose reads: `TELEGRAM_BOT_TOKEN`,
`ADMIN_USER_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `LLM_PROVIDER`,
`LLM_API_KEY`.

### Spool protocol

Filesystem only, no SSH, a single writer on each side.

- **Job** — bot writes `jobs/<id>.job.json`:
  `{ "media": ["<token>/videos/file_123.mp4", ...] }`. Paths are relative to the
  `tg-data` root, so they resolve on both the bot container and the host.
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
forward video/audio       → API server saves file under tg-data → path appended to batch.media
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

## Error handling

| Situation | Behaviour |
|---|---|
| Message from non-whitelisted user | Ignored silently |
| File rejected by local server (too big / unsupported) | Error message to user; item not added to the batch |
| One media item fails transcription | Worker sets `error` and leaves `transcript_path` null; bot notes which item failed, continues with the rest, mentions it in the final message; a later `/go` retries just that item |
| All media items fail | Report failure; `/go` retries |
| Worker not running / Docker can't see the spool | Job file sits in `jobs/` untouched; the bot reports "queued" and keeps waiting. It runs when the worker is back — same tolerance as the laptop being asleep |
| Malformed job file | Worker writes a `result.json` with every item errored; bot reports the batch failed, `/go` retries |
| LLM API error | Send the raw concatenated transcripts as a document + an error note. The batch is kept either way, so the correction that triggered it can simply be re-sent |
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
- **LLM client**: mocked in all bot tests; a separate thin live check hitting
  Grok with a trivial prompt, run manually.
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
- Local MLX LLM backend (M5 Pro / 48 GB can host it later; Grok first)
