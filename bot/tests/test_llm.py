import pytest

from src.llm import LLMError, run_prompt


def test_unknown_provider_is_rejected_before_any_network_call():
    with pytest.raises(LLMError, match="unsupported LLM provider"):
        run_prompt("s", "t", api_key="k", model="m", base_url="u", provider="claude")
