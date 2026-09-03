"""Runtime configuration, read once from the environment at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value


# Each remote provider's endpoint. LLM_BASE_URL overrides it, to point at
# another OpenAI-compatible host. "local" has none: it runs the model on the
# host worker through the spool, not over HTTP.
_PROVIDER_BASE_URL = {"grok": "https://api.x.ai/v1"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _user_ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(",", " ").split())


@dataclass(frozen=True)
class Config:
    bot_token: str
    # Numeric Telegram ids allowed to use the bot; every other sender is ignored.
    allowed_user_ids: frozenset[int]
    # my.telegram.org credentials; Telethon needs them even in bot-token mode.
    api_id: int
    api_hash: str
    # Where the bot saves forwarded media; the host worker reads the same tree.
    media_dir: Path
    jobs_dir: Path
    output_dir: Path
    # The host's Hugging Face cache, read-only: the bot only checks whether a
    # local LLM is already there before a job stalls on a multi-GB download.
    models_dir: Path
    metaprompt_path: Path
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    # Strip a reasoning model's <think> block from its reply.
    llm_strip_reasoning: bool
    # Let a reasoning model think before answering. Off: structuring a transcript
    # gains nothing from it, and the chain of thought alone can exhaust the token
    # budget, leaving a reply that is all reasoning and no answer.
    llm_thinking: bool
    result_poll_seconds: float
    # Playlist expansion: cap on videos added, and how long to wait for the
    # worker (serialized behind any running transcription) to enumerate one.
    playlist_max: int
    expand_timeout_seconds: float
    # Seconds of quiet after the last forward before the "batch of N" message.
    debounce_seconds: float


def load_config() -> Config:
    env = os.environ
    provider = env.get("LLM_PROVIDER", "local")
    return Config(
        bot_token=_require("TELEGRAM_BOT_TOKEN"),
        allowed_user_ids=_user_ids(_require("ALLOWED_USER_IDS")),
        api_id=int(_require("TELEGRAM_API_ID")),
        api_hash=_require("TELEGRAM_API_HASH"),
        media_dir=Path(env.get("MEDIA_DIR", "/app/media")),
        jobs_dir=Path(env.get("JOBS_DIR", "/app/jobs")),
        output_dir=Path(env.get("OUTPUT_DIR", "/app/output")),
        models_dir=Path(env.get("MODELS_DIR", "/app/models")),
        metaprompt_path=Path(env.get("METAPROMPT_PATH", "/app/prompts/default.md")),
        llm_provider=provider,
        # A model running on the host needs no key.
        llm_api_key="" if provider == "local" else _require("LLM_API_KEY"),
        # For "local" this is what mlx-lm loads: a Hugging Face repo id or a
        # path on the host, not a name resolved here.
        llm_model=env.get("LLM_MODEL", "grok-beta"),
        llm_base_url=env.get("LLM_BASE_URL") or _PROVIDER_BASE_URL.get(provider, ""),
        llm_strip_reasoning=_env_bool("LLM_STRIP_REASONING", True),
        llm_thinking=_env_bool("LLM_THINKING", False),
        result_poll_seconds=float(env.get("RESULT_POLL_SECONDS", "2")),
        playlist_max=int(env.get("PLAYLIST_MAX", "100")),
        expand_timeout_seconds=float(env.get("EXPAND_TIMEOUT_SECONDS", "300")),
        debounce_seconds=float(env.get("DEBOUNCE_SECONDS", "2")),
    )
