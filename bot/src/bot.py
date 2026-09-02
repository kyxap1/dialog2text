"""Telegram bot: whitelist, batch state, command/text routing, job queue."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import llm
from .batch import Batch
from .config import Config
from .spool import await_result, new_job_id, write_job
from .paths import tg_path_to_relative
from .stage2 import build_system_prompt, build_transcript_text

log = logging.getLogger("bot")

PREVIEW_CHARS = 800


class BotApp:
    """Holds the batch and the single job queue; methods are the handlers."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.batch = Batch()
        self.queue: asyncio.Queue = asyncio.Queue()

    async def start_queue_worker(self, application: Application) -> None:
        application.create_task(self._run_queue())

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

    async def on_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        obj = (
            message.video
            or message.audio
            or message.voice
            or message.video_note
            or message.document
        )
        try:
            tg_file = await context.bot.get_file(obj.file_id)
        except TelegramError as exc:
            await message.reply_text(f"File rejected: {exc}")
            return
        rel = tg_path_to_relative(tg_file.file_path, self.cfg.api_root)
        item = self.batch.add_media(rel)
        await message.reply_text(f"Added [{item.index}] {Path(rel).name}")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        kind = self.batch.add_text(update.message.text)
        if kind == "prompt":
            await update.message.reply_text("Added to the prompt.")
            return
        position = self.queue.qsize()
        await self.queue.put(lambda: self._stage2_job(update, context))
        await update.message.reply_text(
            f"Correction queued (position {position}); re-running the summary."
        )

    async def on_go(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        position = self.queue.qsize()
        await self.queue.put(lambda: self._go_job(update, context))
        await update.message.reply_text(f"Queued (position {position}).")

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.batch.reset()
        await update.message.reply_text("Batch cleared.")

    # -- jobs -----------------------------------------------------------

    async def _go_job(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pending = self.batch.untranscribed()
        if pending:
            job_id = new_job_id()
            write_job(self.cfg.jobs_dir, job_id, [m.filename for m in pending])
            await update.message.reply_text(
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
            await update.message.reply_text("Stage 1:\n" + "\n".join(lines))

        if not self.batch.media:
            await update.message.reply_text("Batch is empty.")
            return
        if all(m.transcript_path is None for m in self.batch.media):
            await update.message.reply_text("Every item failed transcription. /go retries.")
            return
        await self._stage2_job(update, context)

    async def _stage2_job(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            )
        except llm.LLMError as exc:
            await self._send_document(update, context, "transcripts.md", text)
            await update.message.reply_text(
                f"LLM error: {exc}\nSent the raw transcripts; re-send the correction to retry."
            )
            return
        self.batch.has_output = True
        await self._send_document(update, context, "summary.md", summary)
        await update.message.reply_text(summary[:PREVIEW_CHARS])

    async def _send_document(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        filename: str,
        content: str,
    ) -> None:
        buf = BytesIO(content.encode())
        buf.name = filename
        await context.bot.send_document(
            chat_id=update.effective_chat.id, document=buf, filename=filename
        )


def build_application(cfg: Config) -> Application:
    application = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .base_url(f"{cfg.api_base_url}/bot")
        .base_file_url(f"{cfg.api_base_url}/file/bot")
        .local_mode(True)
        .build()
    )
    state = BotApp(cfg)
    application.bot_data["state"] = state  # keep a reference; also aids testing
    only_me = filters.User(user_id=cfg.admin_user_id)
    media = (
        filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.Document.ALL
    )
    application.add_handler(CommandHandler("go", state.on_go, filters=only_me))
    application.add_handler(CommandHandler("reset", state.on_reset, filters=only_me))
    application.add_handler(MessageHandler(only_me & media, state.on_media))
    application.add_handler(
        MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, state.on_text)
    )
    application.post_init = state.start_queue_worker
    return application
