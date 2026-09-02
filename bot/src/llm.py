"""LLM client. One Grok (xAI, OpenAI-compatible) implementation for now."""

from __future__ import annotations

from openai import OpenAI


class LLMError(RuntimeError):
    pass


def run_prompt(
    system: str,
    text: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    provider: str = "grok",
) -> str:
    if provider != "grok":
        raise LLMError(f"unsupported LLM provider: {provider}")
    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
    except Exception as exc:  # any API failure is reported the same way upstream
        raise LLMError(str(exc)) from exc
    return response.choices[0].message.content or ""
