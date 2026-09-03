"""LLM client for a remote OpenAI-compatible API (Grok).

`LLM_PROVIDER=local` does not come here: MLX needs the Mac GPU, so the bot
hands stage 2 to the host worker over the spool."""

from __future__ import annotations

from openai import OpenAI

# Providers reachable over HTTP. "local" is deliberately absent: it never gets
# here, and calling it as an endpoint would silently talk to nothing.
OPENAI_COMPATIBLE = ("grok",)


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
    strip_reasoning: bool = True,
) -> str:
    if provider not in OPENAI_COMPATIBLE:
        raise LLMError(f"unsupported LLM provider: {provider}")
    # A keyless endpoint still needs a non-empty key for the OpenAI SDK.
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
    return strip_think(content) if strip_reasoning else content


def strip_think(text: str) -> str:
    """Reasoning models emit their chain of thought before the answer, wrapped
    in a think tag (`</think>` is the DeepSeek-R1 convention most open models
    follow; `</thinking>` is the other spelling). Neither the remote API nor
    mlx-lm splits it into a separate field, so drop everything up to the last
    closing tag here.
    """
    for marker in ("</think>", "</thinking>"):
        if marker in text:
            return text.split(marker)[-1].lstrip()
    return text
