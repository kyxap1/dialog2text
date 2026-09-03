"""Telegram bot (Telethon/MTProto): whitelist, batch state, routing, job queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telethon import Button, TelegramClient, events
from telethon.sessions import MemorySession

from . import llm, status
from .batch import Batch
from .config import Config
from .spool import (
    await_llm_result,
    await_result,
    new_job_id,
    write_job,
    write_llm_job,
)
from .stage2 import build_system_prompt, build_transcript_text

log = logging.getLogger("bot")


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"

PREVIEW_CHARS = 800


URL_CACHE_NAME = "url-cache.json"


def _youtube_id(url: str) -> str | None:
    """Video id for a watch or youtu.be link, else None. ponytail: no shorts/live."""
    u = urlparse(url)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        return u.path.lstrip("/").split("/")[0] or None
    if host == "youtube.com" or host.endswith(".youtube.com"):
        return (parse_qs(u.query).get("v") or [None])[0]
    return None


def _load_url_cache(output_dir: Path) -> dict[str, str]:
    # Written by the host worker after each URL transcription; read-only here.
    try:
        return json.loads((output_dir / URL_CACHE_NAME).read_text())
    except (OSError, ValueError):
        return {}


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Shown under every reply; the buttons just send these commands as text.
KEYBOARD = [
    [
        Button.text("/queue", resize=True),
        Button.text("/go", resize=True),
        Button.text("/retry", resize=True),
    ]
]


class BotApp:
    """Holds the per-sender batches and the single job queue; methods are the handlers."""

    def __init__(self, cfg: Config, client: TelegramClient | None = None):
        self.cfg = cfg
        self.client = client
        # One batch per sender; the host worker and the LLM stay a single queue.
        self.batches: dict[int, Batch] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: asyncio.Task | None = None
        # Set while a summary waits on a model that was not on disk, so /status
        # knows to poll the download instead of hitting the HF API every time.
        self.downloading = False

    def _batch(self, event) -> Batch:
        return self.batches.setdefault(event.sender_id, Batch())

    def _batch_for_new_item(self, event) -> Batch:
        # The first item added after a finished summary starts a fresh batch.
        batch = self._batch(event)
        if batch.has_output:
            batch.reset()
        return batch

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
        # A forwarded album drops photos in alongside the clip; the mime type is
        # in the update already, so skip non-media silently before downloading.
        if not (f.mime_type or "").startswith(("audio/", "video/")):
            return
        self.cfg.media_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.media_dir / f".{event.sender_id}_{msg.id}.part"
        try:
            await msg.download_media(file=str(tmp))
        except Exception as exc:  # a 2 GB download can fail mid-stream
            log.exception("download failed")
            tmp.unlink(missing_ok=True)
            await self._reply(event, f"Download failed: {exc}")
            return
        # Telegram filenames are unusable (any language, often absent), so the
        # file is content-addressed. sender+msg keep the name unique per user.
        name = f"{event.sender_id}_{msg.id}_{_sha1(tmp)[:12]}{f.ext or ''}"
        os.replace(tmp, self.cfg.media_dir / name)
        item = self._batch_for_new_item(event).add_media(name)
        await self._reply(event, f"Added [{item.index}] {name}")

    async def on_status(self, event) -> None:
        lines = [f"Queue: {self.queue.qsize()} job(s) waiting."]
        download = await self._download_status() if self.downloading else None
        if download:
            done, total = download["bytes"], download["total"]
            lines.append(
                f"{download['model']}: {_gb(done)} of {_gb(total)}"
                f" ({done * 100 // total}%)"
            )
        await self._reply(event, "\n".join(lines))

    async def _download_status(self) -> dict | None:
        if self.cfg.llm_provider != "local":
            return None
        return await asyncio.to_thread(
            status.model_download, self.cfg.models_dir, self.cfg.llm_model
        )

    async def on_queue(self, event) -> None:
        await self._reply(event, self._batch_lines(self._batch(event)))

    def _batch_lines(self, batch: Batch) -> str:
        if not batch.media:
            return "Batch is empty."
        return "\n".join(
            f"[{m.index}] {m.filename} — "
            + ("transcribed" if m.transcript_path else "pending")
            for m in batch.media
        )

    async def on_text(self, event) -> None:
        text = event.raw_text.strip()
        # A lone URL is a transcription source, but only YouTube (run.sh feeds it
        # to yt-dlp); a URL inside a sentence stays a prompt/correction.
        if len(text.split()) == 1 and text.startswith(("http://", "https://")):
            vid = _youtube_id(text)
            if vid is None:
                await self._reply(event, "Only YouTube links are supported.")
                return
            url = f"https://www.youtube.com/watch?v={vid}"
            batch = self._batch_for_new_item(event)
            was = len(batch.media)
            item = batch.add_media(url)
            if len(batch.media) == was:
                await self._reply(event, f"[{item.index}] already in the batch.")
            else:
                await self._reply(event, f"Added [{item.index}] {url}\nSend /go to transcribe.")
            return
        kind = self._batch(event).add_text(event.raw_text)
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
        self._batch(event).reset()
        await self._reply(event, "Batch cleared.")

    # -- jobs -----------------------------------------------------------

    def _resolve_cached(self, batch: Batch) -> int:
        """Fill transcript_path for URLs already transcribed on a past run."""
        cache = _load_url_cache(self.cfg.output_dir)
        hits = 0
        for m in batch.media:
            path = cache.get(m.filename) if m.transcript_path is None else None
            if path and (self.cfg.output_dir / path).exists():
                m.transcript_path = path
                hits += 1
        return hits

    async def _go_job(self, event) -> None:
        batch = self._batch(event)
        cached = self._resolve_cached(batch)
        if cached:
            await event.respond(f"{cached} already transcribed (cached).")
        pending = batch.untranscribed()
        if pending:
            job_id = new_job_id()
            write_job(self.cfg.jobs_dir, job_id, [m.filename for m in pending])
            await event.respond(
                f"Transcribing {len(pending)} item(s) on the host worker; waiting…"
            )
            results = await await_result(
                self.cfg.jobs_dir, job_id, self.cfg.result_poll_seconds
            )
            batch.merge_results(results)
            lines = []
            for result in results:
                ok = bool(result.get("transcript_path"))
                mark = "ok" if ok else f"FAILED ({result.get('error')})"
                lines.append(f"[{result['index']}] {result['name']} — {mark}")
            await event.respond("Stage 1:\n" + "\n".join(lines))

        if not batch.media:
            await event.respond("Batch is empty.")
            return
        if all(m.transcript_path is None for m in batch.media):
            await event.respond("Every item failed transcription. /go retries.")
            return
        if not pending:
            await event.respond("Re-running the summary…")
        await self._stage2_job(event)

    async def _run_llm(self, system: str, text: str) -> str:
        # A local model runs on the host GPU, so it goes through the same spool
        # as transcription; only a remote API is callable from the container.
        if self.cfg.llm_provider != "local":
            return await asyncio.to_thread(
                llm.run_prompt,
                system,
                text,
                api_key=self.cfg.llm_api_key,
                model=self.cfg.llm_model,
                base_url=self.cfg.llm_base_url,
                provider=self.cfg.llm_provider,
                strip_reasoning=self.cfg.llm_strip_reasoning,
            )
        job_id = new_job_id()
        write_llm_job(
            self.cfg.jobs_dir,
            job_id,
            self.cfg.llm_model,
            system,
            text,
            self.cfg.llm_thinking,
        )
        result = await await_llm_result(
            self.cfg.jobs_dir, job_id, self.cfg.result_poll_seconds
        )
        if result.get("error"):
            raise llm.LLMError(result["error"])
        summary = result["summary"]
        return llm.strip_think(summary) if self.cfg.llm_strip_reasoning else summary

    async def _stage2_job(self, event) -> None:
        batch = self._batch(event)
        text = build_transcript_text(batch.media, self.cfg.output_dir)
        system = build_system_prompt(self.cfg.metaprompt_path, batch)
        download = await self._download_status()
        if download:
            self.downloading = True
            await event.respond(
                f"{download['model']} is not on disk yet — downloading "
                f"{_gb(download['total'])} first, once. /status shows the progress."
            )
        try:
            summary = await self._run_llm(system, text)
        except llm.LLMError as exc:
            self.downloading = False
            await self._send_document(event, "transcripts.md", text)
            await event.respond(
                f"LLM error: {exc}\nSent the raw transcripts; re-send the correction to retry."
            )
            return
        self.downloading = False
        batch.has_output = True
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
    mine = lambda e: e.sender_id in cfg.allowed_user_ids  # noqa: E731

    handlers = [
        (state.on_start, events.NewMessage(pattern=r"^/start$", func=mine)),
        # A reply-keyboard button sends its label verbatim, so /retry just
        # routes to /go — transcription is idempotent, /go re-runs only failures.
        (state.on_go, events.NewMessage(pattern=r"^/(go|retry)$", func=mine)),
        (state.on_queue, events.NewMessage(pattern=r"^/queue$", func=mine)),
        (state.on_status, events.NewMessage(pattern=r"^/status$", func=mine)),
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
