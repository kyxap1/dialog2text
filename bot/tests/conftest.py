from pathlib import Path

import pytest

from src.config import Config


class FakeFile:
    def __init__(self, file_path: str):
        self.file_path = file_path


class FakeBot:
    def __init__(self, file_path="/var/lib/telegram-bot-api/tok/videos/x.mp4"):
        self._file_path = file_path
        self.documents: list[tuple[int, str]] = []

    async def get_file(self, file_id):
        return FakeFile(self._file_path)

    async def send_document(self, chat_id, document, filename):
        self.documents.append((chat_id, filename))


class FakeMessage:
    def __init__(self, text=None, video=None):
        self.text = text
        self.video = video
        self.audio = self.voice = self.video_note = self.document = None
        self.replies: list[str] = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeChat:
    id = 42


class FakeUpdate:
    def __init__(self, message: FakeMessage):
        self.message = message
        self.effective_chat = FakeChat()


class FakeContext:
    def __init__(self, bot: FakeBot):
        self.bot = bot


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    metaprompt = tmp_path / "default.md"
    metaprompt.write_text("BASE PROMPT")
    return Config(
        bot_token="t",
        admin_user_id=1,
        api_base_url="http://x:8081",
        api_root=Path("/var/lib/telegram-bot-api"),
        jobs_dir=tmp_path / "jobs",
        output_dir=tmp_path / "output",
        metaprompt_path=metaprompt,
        llm_provider="grok",
        llm_api_key="k",
        llm_model="grok-test",
        llm_base_url="http://x/v1",
        result_poll_seconds=0.01,
    )
