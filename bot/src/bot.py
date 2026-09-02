"""Telegram bot (Telethon/MTProto): whitelist, batch state, routing, job queue."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.sessions import MemorySession

from . import llm
from .batch import Batch
from .config import Config
from .spool import await_result, new_job_id, write_job
from .stage2 import build_system_prompt, build_transcript_text

log = logging.getLogger("bot")

PREVIEW_CHARS = 800

# Shown under every reply; the buttons just send these commands as text.
KEYBOARD = [[Button.text("/queue", resize=True), Button.text("/go", resize=True)]]


class BotApp:
    """Holds the batch and the single job queue; methods are the handlers."""

    def __init__(self, cfg: Config, client: TelegramClient | None = None):
        self.cfg = cfg
        self.client = client
        self.batch = Batch()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: asyncio.Task | None = None

    def start_queue(self) -> None:
        self._queue_task = asyncio.create_task(self._run_queue())

    async def _run_queue(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await job()
            except Exception:  # a failed job must not kill the queue
                log.exception("queued job failed")
            finally:
                self.queue.task_done()

    # -- handlers ---------------------------------------------------------

    async def _reply(self, event, text: str) -> None:
        # Every reply re-sends the keyboard so the /queue /go buttons stay put.
        await event.respond(text, buttons=KEYBOARD)

    async def on_start(self, event) -> None:
        await self._reply(event, "Forward media, then /go.")

    async def on_media(self, event) -> None:
        msg = event.message
        f = msg.file
        if f is None:
            return
        name = f.name or f"{msg.id}{f.ext or ''}"
        # Per-message subdir keeps the media path token-free and unique; the
        # basename stays clean for the status lines and stage-2 headings.
        # ponytail: two forwards with the same filename collide in output/;
        # prefix the name with msg.id if that ever bites.
        dest = self.cfg.media_dir / str(msg.id) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await msg.download_media(file=str(dest))
        except Exception as exc:  # a 2 GB download can fail mid-stream
            log.exception("download failed")
            await self._reply(event, f"Download failed: {exc}")
            return
        item = self.batch.add_media(f"{msg.id}/{name}")
        await self._reply(event, f"Added [{item.index}] {name}")

    async def on_queue(self, event) -> None:
        await self._reply(event, self._batch_lines())

    def _batch_lines(self) -> str:
        if not self.batch.media:
            return "Batch is empty."
        return "\n".join(
            f"[{m.index}] {Path(m.filename).name} — "
            + ("transcribed" if m.transcript_path else "pending")
            for m in self.batch.media
        )

    async def on_text(self, event) -> None:
        kind = self.batch.add_text(event.raw_text)
        if kind == "prompt":
            await self._reply(event, "Added to the prompt.")
            return
        position = self.queue.qsize()
        await self.queue.put(lambda: self._stage2_job(event))
        await self._reply(
            event, f"Correction queued (position {position}); re-running the summary."
        )

    async def on_go(self, event) -> None:
        position = self.queue.qsize()
        await self.queue.put(lambda: self._go_job(event))
        await self._reply(event, f"Queued (position {position}).")

    async def on_reset(self, event) -> None:
        self.batch.reset()
        await self._reply(event, "Batch cleared.")

    # -- jobs -----------------------------------------------------------

    async def _go_job(self, event) -> None:
        pending = self.batch.untranscribed()
        if pending:
            job_id = new_job_id()
            write_job(self.cfg.jobs_dir, job_id, [m.filename for m in pending])
            await event.respond(
                f"Transcribing {len(pending)} item(s) on the host worker; waiting…"
            )
            results = await await_result(
                self.cfg.jobs_dir, job_id, self.cfg.result_poll_seconds
            )
            self.batch.merge_results(results)
            lines = []
            for result in results:
                ok = bool(result.get("transcript_path"))
                mark = "ok" if ok else f"FAILED ({result.get('error')})"
                lines.append(f"[{result['index']}] {result['name']} — {mark}")
            await event.respond("Stage 1:\n" + "\n".join(lines))

        if not self.batch.media:
            await event.respond("Batch is empty.")
            return
        if all(m.transcript_path is None for m in self.batch.media):
            await event.respond("Every item failed transcription. /go retries.")
            return
        await self._stage2_job(event)

    async def _stage2_job(self, event) -> None:
        text = build_transcript_text(self.batch.media, self.cfg.output_dir)
        system = build_system_prompt(self.cfg.metaprompt_path, self.batch)
        try:
            summary = await asyncio.to_thread(
                llm.run_prompt,
                system,
                text,
                api_key=self.cfg.llm_api_key,
                model=self.cfg.llm_model,
                base_url=self.cfg.llm_base_url,
                provider=self.cfg.llm_provider,
                strip_reasoning=self.cfg.llm_strip_reasoning,
            )
        except llm.LLMError as exc:
            await self._send_document(event, "transcripts.md", text)
            await event.respond(
                f"LLM error: {exc}\nSent the raw transcripts; re-send the correction to retry."
            )
            return
        self.batch.has_output = True
        await self._send_document(event, "summary.md", summary)
        await event.respond(summary[:PREVIEW_CHARS])

    async def _send_document(self, event, filename: str, content: str) -> None:
        buf = BytesIO(content.encode())
        buf.name = filename
        await event.client.send_file(event.chat_id, buf, force_document=True)


def build_client(cfg: Config) -> tuple[TelegramClient, BotApp]:
    # ponytail: MemorySession — bot re-auths on restart (instant) and loses
    # update catch-up across a restart; swap in a file session on a volume if
    # missed-while-down messages ever matter.
    client = TelegramClient(MemorySession(), cfg.api_id, cfg.api_hash)
    state = BotApp(cfg, client)
    mine = lambda e: e.sender_id == cfg.admin_user_id  # noqa: E731

    handlers = [
        (state.on_start, events.NewMessage(pattern=r"^/start$", func=mine)),
        (state.on_go, events.NewMessage(pattern=r"^/go$", func=mine)),
        (state.on_queue, events.NewMessage(pattern=r"^/queue$", func=mine)),
        (state.on_reset, events.NewMessage(pattern=r"^/reset$", func=mine)),
        (state.on_media, events.NewMessage(func=lambda e: mine(e) and e.message.file is not None)),
        (
            state.on_text,
            events.NewMessage(
                func=lambda e: mine(e)
                and e.message.file is None
                and bool(e.raw_text)
                and not e.raw_text.startswith("/")
            ),
        ),
    ]
    for callback, event in handlers:
        client.add_event_handler(callback, event)
    return client, state
