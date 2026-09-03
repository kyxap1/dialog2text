from pathlib import Path

import pytest

from src.config import Config


class FakeFile:
    def __init__(self, name="x.mp4", ext=".mp4", mime_type="video/mp4"):
        self.name = name
        self.ext = ext
        self.mime_type = mime_type


class FakeClient:
    def __init__(self):
        self.files: list[tuple[int, str]] = []

    async def send_file(self, chat_id, file, **kwargs):
        self.files.append((chat_id, getattr(file, "name", None)))


class FakeMessage:
    def __init__(self, text="", file=None, msg_id=1):
        self.raw_text = text
        self.file = file
        self.id = msg_id
        self.media = object() if file is not None else None

    async def download_media(self, file):
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        Path(file).write_bytes(b"fake")
        return file


class FakeEvent:
    def __init__(self, message: FakeMessage, client=None, sender_id=1, chat_id=42):
        self.message = message
        self.raw_text = message.raw_text
        self.client = client or FakeClient()
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.responses: list[str] = []

    async def respond(self, text, buttons=None):
        self.responses.append(text)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    metaprompt = tmp_path / "default.md"
    metaprompt.write_text("BASE PROMPT")
    return Config(
        bot_token="t",
        allowed_user_ids=frozenset({1}),
        api_id=1,
        api_hash="h",
        media_dir=tmp_path / "media",
        jobs_dir=tmp_path / "jobs",
        output_dir=tmp_path / "output",
        models_dir=tmp_path / "models",
        metaprompt_path=metaprompt,
        llm_provider="grok",
        llm_api_key="k",
        llm_model="grok-test",
        llm_base_url="http://x/v1",
        llm_strip_reasoning=True,
        llm_thinking=False,
        result_poll_seconds=0.01,
    )
