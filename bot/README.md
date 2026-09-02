# Telegram transcription bot

A personal Telegram bot that turns forwarded video/audio into one structured
summary. Forward media, send `/go`, get a `.md` back. Any text message after
that is a correction and re-runs the summary.

Design: [`../docs/superpowers/specs/2026-09-01-telegram-transcription-bot-design.md`](../docs/superpowers/specs/2026-09-01-telegram-transcription-bot-design.md).

## How it runs

Transcription (`run.sh`, whisper-mlx + pyannote) needs Apple Metal, so it stays
on the host. The bot and the local Telegram API server run under Docker Compose.
The LLM pass calls either a remote API (Grok) or a local model served on the
host by Docker Model Runner — see "LLM backend" below.

```
Telegram ── long poll ──> bot container ──> jobs/*.job.json ──> worker.sh (host) ──> run.sh
                              │                                       │
                              │ <──────────── jobs/*.result.json <─────┘
                              ▼
                     LLM pass (Grok API │ local model) ──> summary.md
```

`jobs/` and `tg-data/` (media the local API server writes) are shared through
bind mounts.

## Setup

1. **Config.** `cp bot/.env.example bot/.env` and fill it in:
   - `TELEGRAM_BOT_TOKEN` — @BotFather → `/newbot`
   - `ADMIN_USER_ID` — your numeric id (@userinfobot); only this user is served
   - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — https://my.telegram.org → API
     development tools (lets the local server fetch files over 20 MB)
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
   (e.g. `grok-4`, `grok-3`, `grok-3-mini`). The `.env.example` default
   (`grok-beta`) is a placeholder.
5. Verify: `curl https://api.x.ai/v1/models -H "Authorization: Bearer $LLM_API_KEY"`.

### Local model (`LLM_PROVIDER=local`, no API key)

Docker Model Runner serves an MLX model on the Metal GPU and unloads it ~5 min
after the last request, so it only holds RAM around a `/go` or a correction.
Needs Docker Desktop 4.62+.

1. One-time host setup (the `hf.co/` prefix is required — without it Docker
   looks on Docker Hub and the pull fails):

   ```bash
   docker model install-runner --backend vllm
   docker model pull hf.co/mlx-community/Qwen3.8-27B-4bit
   ```

   `Qwen3.8-27B-4bit` is ~16 GB in RAM with a 262K context. For better quality
   at ~28 GB use `hf.co/mlx-community/Qwen3.8-27B-8bit`. Browse
   <https://huggingface.co/mlx-community>.

2. In `bot/.env` (use the exact name from `docker model ls` for `LLM_MODEL`):

   ```
   LLM_PROVIDER=local
   LLM_BASE_URL=http://host.docker.internal:12434/engines/v1
   LLM_MODEL=hf.co/mlx-community/Qwen3.8-27B-4bit
   ```

3. Verify (from the host): `curl http://localhost:12434/engines/v1/models`.

## Commands

| | |
|---|---|
| forward video/audio | added to the batch |
| text (before first summary) | added to the prompt |
| `/go` | transcribe new items, then summarise; the batch is kept |
| text (after a summary) | correction — re-runs the summary with all corrections so far |
| `/reset` | clear the batch |

## Tests

```bash
cd bot && docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  bash -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

Host worker check (needs the repo `.venv`): `./worker_selfcheck.sh` from the root.
