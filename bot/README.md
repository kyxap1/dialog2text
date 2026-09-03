# Telegram transcription bot

A personal Telegram bot that turns forwarded video/audio into one structured
summary. Forward media, send `/go`, get a `.md` back. Any text message after
that is a correction and re-runs the summary; the next forwarded file starts a
fresh batch. Serves a small whitelist of user ids, each with its own batch.

Design: [`../docs/superpowers/specs/2026-09-01-telegram-transcription-bot-design.md`](../docs/superpowers/specs/2026-09-01-telegram-transcription-bot-design.md).

## How it runs

Transcription (`run.sh`, whisper-mlx + pyannote) needs Apple Metal, so it stays
on the host. The bot runs under Docker Compose and talks to Telegram over
MTProto (Telethon), which lifts the file-download limit to 2 GB. The LLM pass
either calls a remote API (Grok) from the container or, for a local model, goes
back to the host worker — MLX needs Metal too. See "LLM backend" below.

```
Telegram ── MTProto ──> bot container ──> jobs/*.job.json ──> worker.sh (host) ──> run.sh
                            │                                       │
                            │ <──────────── jobs/*.result.json <─────┘
                            ▼
                   LLM pass ──> summary.md
                     ├── Grok API, called from the container
                     └── local model: jobs/*.llm.json ──> worker.sh ──> mlx-lm
```

The bot saves each forwarded file as `media/<sender-id>_<message-id>_<hash>.<ext>`;
`media/`, `jobs/` and `output/` are shared with the host worker through bind
mounts.

## Setup

1. **Config.** `cp bot/.env.example bot/.env` and fill it in:
   - `TELEGRAM_BOT_TOKEN` — @BotFather → `/newbot`
   - `ADMIN_USER_ID` — your numeric id (@userinfobot); only this user is served
   - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — https://my.telegram.org → API
     development tools (Telethon needs them even in bot-token mode)
   - `LLM_PROVIDER` and its settings — see "LLM backend" below

2. **Host worker** (from the repo root):

   ```bash
   sed "s#REPO_PATH#$PWD#g" bot/deploy/com.whisper-mlx.worker.plist \
     > ~/Library/LaunchAgents/com.whisper-mlx.worker.plist
   launchctl load ~/Library/LaunchAgents/com.whisper-mlx.worker.plist
   ```

3. **The stack:**

   ```bash
   cd bot && docker compose up -d
   ```

   Enable "Start Docker Desktop on login" so it comes back after a reboot.

## LLM backend

Set `LLM_PROVIDER` to `grok` (a remote API) or `local` (a model on this Mac).

### Remote API (`LLM_PROVIDER=grok`)

Works with Grok or any OpenAI-compatible host (`LLM_BASE_URL`, `LLM_MODEL`,
`LLM_API_KEY`). For Grok:

1. Sign in at <https://console.x.ai> with your X account.
2. **Create an API key**: left sidebar → *API Keys* → *Create API Key*. Copy it
   (shown once) into `LLM_API_KEY`.
3. A key belongs to a **team**; the team needs a payment method or credits on
   its *Billing* page before requests succeed. xAI has run promotional free
   monthly credits — confirm the current terms on the billing page yourself.
4. **Model name**: check <https://docs.x.ai/docs/models> and set `LLM_MODEL`
   (e.g. `grok-4`, `grok-3`, `grok-3-mini`).
5. Verify: `curl https://api.x.ai/v1/models -H "Authorization: Bearer $LLM_API_KEY"`.

### Local model (`LLM_PROVIDER=local`, no API key)

The bot writes `jobs/<id>.llm.json`; `worker.sh` runs the model with mlx-lm on
the Metal GPU and writes the result back. Nothing serves the model between
summaries — the weights are loaded per job (~10 s warm, ~30 s cold) and freed
after. Docker on macOS passes no GPU through, which is why this runs host-side
like transcription does.

1. In `bot/.env`:

   ```
   LLM_PROVIDER=local
   LLM_MODEL=mlx-community/Qwen3.8-27B-4bit
   ```

   `LLM_MODEL` is what mlx-lm loads: a Hugging Face repo id, downloaded into
   `models/` on first use (`worker.sh` sets `HF_HUB_CACHE`), or a path on the
   host. Nothing to provision by hand — the first summary pays the download.
   This model needs ~16 GB of RAM while it runs; `-8bit` is ~28 GB. Browse
   <https://huggingface.co/mlx-community>.

2. Verify from the repo root:

   ```bash
   HF_HUB_CACHE=$PWD/models .venv/bin/mlx_lm.generate \
     --model mlx-community/Qwen3.8-27B-4bit --prompt "Say ok" --max-tokens 16
   ```

`LLM_MAX_TOKENS` (default 8192) caps a summary's length; the worker reads it.

## Commands

| | |
|---|---|
| forward video/audio | added to the batch (the first one after a summary starts a fresh batch) |
| text (before first summary) | added to the prompt |
| `/go` | transcribe new items, then summarise; the batch is kept |
| `/retry` | re-run transcription for items that failed |
| text (after a summary) | correction — re-runs the summary with all corrections so far |
| `/reset` | clear the batch |

## Tests

```bash
cd bot && docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  bash -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

Host worker check (needs the repo `.venv`): `./worker_selfcheck.sh` from the root.
