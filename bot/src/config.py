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


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_user_id: int
    api_base_url: str
    # Prefix the local telegram-bot-api server puts on the paths it reports;
    # stripped to get a path relative to the shared tg-data root.
    api_root: Path
    jobs_dir: Path
    output_dir: Path
    metaprompt_path: Path
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    result_poll_seconds: float


def load_config() -> Config:
    env = os.environ
    return Config(
        bot_token=_require("TELEGRAM_BOT_TOKEN"),
        admin_user_id=int(_require("ADMIN_USER_ID")),
        api_base_url=env.get("TELEGRAM_API_BASE_URL", "http://telegram-bot-api:8081"),
        api_root=Path(env.get("TELEGRAM_API_ROOT", "/var/lib/telegram-bot-api")),
        jobs_dir=Path(env.get("JOBS_DIR", "/app/jobs")),
        output_dir=Path(env.get("OUTPUT_DIR", "/app/output")),
        metaprompt_path=Path(env.get("METAPROMPT_PATH", "/app/prompts/default.md")),
        llm_provider=env.get("LLM_PROVIDER", "grok"),
        llm_api_key=_require("LLM_API_KEY"),
        llm_model=env.get("LLM_MODEL", "grok-beta"),
        llm_base_url=env.get("LLM_BASE_URL", "https://api.x.ai/v1"),
        result_poll_seconds=float(env.get("RESULT_POLL_SECONDS", "2")),
    )
