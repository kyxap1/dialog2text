"""LLM client. One OpenAI-compatible call, used for both a remote API (Grok)
and a local model served by Docker Model Runner (`LLM_PROVIDER=local`)."""

from __future__ import annotations

from openai import OpenAI

# Both are chat-completions over an OpenAI-compatible endpoint; only the
# base URL / key / model differ, and those come from config.
OPENAI_COMPATIBLE = ("grok", "local")


class LLMError(RuntimeError):
    pass


def run_prompt(
    system: str,
    text: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    provider: str = "local",
    strip_reasoning: bool = True,
) -> str:
    if provider not in OPENAI_COMPATIBLE:
        raise LLMError(f"unsupported LLM provider: {provider}")
    # Docker Model Runner ignores the key, but the OpenAI SDK requires a non-empty one.
    client = OpenAI(api_key=api_key or "local", base_url=base_url)
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
    content = response.choices[0].message.content or ""
    return _strip_reasoning(content) if strip_reasoning else content


def _strip_reasoning(text: str) -> str:
    """Reasoning models emit their chain of thought before the answer, wrapped
    in a think tag (`</think>` is the DeepSeek-R1 convention most open models
    follow; `</thinking>` is the other spelling). vLLM only splits it into a
    separate field when started with --reasoning-parser, which Docker Model
    Runner does not do, so drop everything up to the last closing tag here.
    """
    for marker in ("</think>", "</thinking>"):
        if marker in text:
            return text.split(marker)[-1].lstrip()
    return text
