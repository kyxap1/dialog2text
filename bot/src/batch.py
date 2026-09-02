"""The single in-memory batch. One user, one batch, lost on restart."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MediaItem:
    index: int
    filename: str
    transcript_path: str | None = None


@dataclass
class Batch:
    media: list[MediaItem] = field(default_factory=list)
    extra_prompt: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    # Set once stage 2 has produced a result; this is what turns a later text
    # message into a correction rather than a prompt addition.
    has_output: bool = False

    def add_media(self, filename: str) -> MediaItem:
        item = MediaItem(index=len(self.media) + 1, filename=filename)
        self.media.append(item)
        return item

    def add_text(self, text: str) -> str:
        """Route a text message. Returns 'correction' or 'prompt'."""
        if self.has_output:
            self.corrections.append(text)
            return "correction"
        self.extra_prompt.append(text)
        return "prompt"

    def untranscribed(self) -> list[MediaItem]:
        return [m for m in self.media if m.transcript_path is None]

    def merge_results(self, results: list[dict]) -> None:
        by_index = {m.index: m for m in self.media}
        for result in results:
            item = by_index.get(result["index"])
            if item is not None and result.get("transcript_path"):
                item.transcript_path = result["transcript_path"]

    def reset(self) -> None:
        self.media.clear()
        self.extra_prompt.clear()
        self.corrections.clear()
        self.has_output = False
