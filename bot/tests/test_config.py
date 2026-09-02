import pytest

from src.config import load_config

BASE = {
    "TELEGRAM_BOT_TOKEN": "t",
    "ADMIN_USER_ID": "1",
}


def test_remote_provider_requires_an_api_key(monkeypatch):
    for k, v in {**BASE, "LLM_PROVIDER": "grok"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        load_config()


def test_local_provider_needs_no_api_key(monkeypatch):
    for k, v in {**BASE, "LLM_PROVIDER": "local"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = load_config()
    assert cfg.llm_provider == "local"
    assert cfg.llm_api_key == ""


def test_base_url_is_derived_from_provider_and_overridable(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    monkeypatch.setenv("LLM_PROVIDER", "grok")
    monkeypatch.setenv("LLM_API_KEY", "k")
    assert load_config().llm_base_url == "https://api.x.ai/v1"

    monkeypatch.setenv("LLM_PROVIDER", "local")
    assert load_config().llm_base_url == "http://host.docker.internal:12434/engines/v1"

    monkeypatch.setenv("LLM_BASE_URL", "http://my-host/v1")
    assert load_config().llm_base_url == "http://my-host/v1"


def test_strip_reasoning_defaults_on_and_takes_false(monkeypatch):
    for k, v in {**BASE, "LLM_PROVIDER": "local"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_STRIP_REASONING", raising=False)
    assert load_config().llm_strip_reasoning is True
    monkeypatch.setenv("LLM_STRIP_REASONING", "false")
    assert load_config().llm_strip_reasoning is False
