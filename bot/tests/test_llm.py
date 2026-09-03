import pytest

from src import llm
from src.llm import LLMError, run_prompt


class _FakeClient:
    """Stand-in for openai.OpenAI: client.chat.completions.create(...)."""

    last: dict = {}
    reply: str = "ok"

    def __init__(self, **kwargs):
        _FakeClient.last["init"] = kwargs
        self.chat = self.completions = self

    def create(self, **kwargs):
        _FakeClient.last["call"] = kwargs
        message = type("M", (), {"content": _FakeClient.reply})
        return type("R", (), {"choices": [type("C", (), {"message": message})]})


def test_unknown_provider_is_rejected_before_any_network_call():
    with pytest.raises(LLMError, match="unsupported LLM provider"):
        run_prompt("s", "t", api_key="k", model="m", base_url="u", provider="claude")


def test_local_provider_is_rejected_here_it_belongs_to_the_worker():
    with pytest.raises(LLMError, match="unsupported LLM provider"):
        run_prompt("s", "t", api_key="", model="m", base_url="u", provider="local")


def test_keyless_host_still_gets_a_non_empty_key(monkeypatch):
    monkeypatch.setattr(llm, "OpenAI", _FakeClient)
    out = run_prompt(
        "s", "t", api_key="", model="some/model",
        base_url="http://h:8080/v1", provider="grok",
    )
    assert out == "ok"
    assert _FakeClient.last["init"]["api_key"]  # SDK never gets an empty key
    assert _FakeClient.last["call"]["model"] == "some/model"


def _run(monkeypatch, reply, **overrides):
    monkeypatch.setattr(llm, "OpenAI", _FakeClient)
    monkeypatch.setattr(_FakeClient, "reply", reply)
    kw = dict(api_key="k", model="m", base_url="u", provider="grok")
    kw.update(overrides)
    return run_prompt("s", "t", **kw)


def test_reasoning_block_is_stripped_by_default(monkeypatch):
    assert _run(monkeypatch, "<think>weigh options</think>\n\nFinal.") == "Final."


def test_reasoning_strip_handles_a_bare_closing_tag(monkeypatch):
    # The chat template injects the opening tag, so the model emits only </think>.
    assert _run(monkeypatch, "reasoning...\n</think>\nAnswer") == "Answer"


def test_reply_without_a_think_tag_is_untouched(monkeypatch):
    assert _run(monkeypatch, "plain answer") == "plain answer"


def test_reasoning_kept_when_strip_disabled(monkeypatch):
    reply = "<think>x</think>\n\nFinal."
    assert _run(monkeypatch, reply, strip_reasoning=False) == reply
