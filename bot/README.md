# Telegram transcription bot

A personal Telegram bot that turns forwarded video/audio into one structured
summary. Forward media, send `/go`, get a `.md` back. Any text message after
that is a correction and re-runs the summary.

Design: [`../docs/superpowers/specs/2026-09-01-telegram-transcription-bot-design.md`](../docs/superpowers/specs/2026-09-01-telegram-transcription-bot-design.md).

## How it runs

Transcription (`run.sh`, whisper-mlx + pyannote) needs Apple Metal, so it stays
on the host. Everything else runs under Docker Compose:

```
Telegram ── long poll ──> bot container ──> jobs/*.job.json ──> worker.sh (host) ──> run.sh
                              │                                       │
                              │ <──────────── jobs/*.result.json <─────┘
                              ▼
                        LLM pass (Grok) ──> summary.md
```

`jobs/` and `tg-data/` (media the local API server writes) are shared through
bind mounts.

## Setup

1. **Config.** `cp bot/.env.example bot/.env` and fill it in:
   - `TELEGRAM_BOT_TOKEN` — @BotFather → `/newbot`
   - `ADMIN_USER_ID` — your numeric id (@userinfobot); only this user is served
   - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — https://my.telegram.org → API
     development tools (lets the local server fetch files over 20 MB)
   - `LLM_API_KEY`, `LLM_MODEL` — see "Grok key" below

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

## Grok key

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
