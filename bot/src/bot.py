"""Telegram bot (Telethon/MTProto): whitelist, batch state, routing, job queue."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.sessions import MemorySession

from . import llm, status, tgmd
from .batch import Batch, MediaItem
from .config import Config
from .core import content, youtube
from .core.debounce import Accumulator
from .spool import (
    await_expand_result,
    await_llm_result,
    await_result,
    new_job_id,
    write_expand_job,
    write_job,
    write_llm_job,
)
from .stage2 import build_system_prompt, build_transcript_text

log = logging.getLogger("bot")


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def _mb(path: Path) -> str:
    # Binary MB (divide by 1024**2), not decimal.
    return f"{path.stat().st_size / 1024**2:.1f} MB"


def _size_prefix(media_dir: Path, filename: str) -> str:
    # filename is a URL for a not-yet-downloaded link item; no size to show.
    path = media_dir / filename
    return f"{_mb(path)} — " if path.exists() else ""


def _doc_name(batch: Batch, suffix: str) -> str:
    """Name the document after the batch's first transcript, like the media is."""
    stem = next(
        (Path(m.transcript_path).stem for m in batch.media if m.transcript_path), "batch"
    )
    return f"{stem}.{suffix}.md"


# Shown under every reply; the buttons just send these commands as text.
KEYBOARD = [
    [
        Button.text("/queue", resize=True),
        Button.text("/prompt", resize=True),
        Button.text("/go", resize=True),
    ],
    [Button.text("/retry", resize=True), Button.text("/reset", resize=True)],
]


class BotApp:
    """Holds the per-sender batches and the single job queue; methods are the handlers.

    User-facing text follows one grammar (Telegram surface only; a REST adapter
    would return structured status instead):
      progress   "<Gerund> …"                 async step underway; trailing "…"
      detection  "batch of N items detected"   head of a multi-item list
      item       "[ref] <label> — <state>"     ref n/N or batch index; state
                                               e.g. new | on disk | added | ok
      failure    "<Step> failed: <reason>"
    """

    def __init__(self, cfg: Config, client: TelegramClient | None = None):
        self.cfg = cfg
        self.client = client
        # One batch per sender; the host worker and the LLM stay a single queue.
        self.batches: dict[int, Batch] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: asyncio.Task | None = None
        # A forward burst (album or loose files) collapses into one announcement.
        self.accumulator = Accumulator(self._announce_batch, cfg.debounce_seconds)
        # Telegram document id (as str) -> content-addressed name, so a re-forward
        # reuses the download. Persisted; grows one entry per distinct file ever
        # forwarded (KBs), stale entries self-heal via the on-disk check below.
        self._media_names: dict[str, str] = content.load_media_index(cfg.output_dir)
        # Set while a summary waits on a model that was not on disk, so /status
        # knows to poll the download instead of hitting the HF API every time.
        self.downloading = False
        # Per-sender chain of commit tasks: downloads race in parallel, but each
        # commit awaits the previous one before touching the batch, so insertion
        # order matches arrival order even when a later, smaller file downloads
        # faster than an earlier, bigger one.
        self._commit_chain: dict[int, asyncio.Task] = {}

    def _batch(self, event) -> Batch:
        return self.batches.setdefault(event.sender_id, Batch())

    def _batch_for_new_item(self, event) -> Batch:
        # The first item added after a finished summary starts a fresh batch,
        # keeping any instruction just typed for this new item.
        batch = self._batch(event)
        if batch.has_output:
            batch.reset(keep_prompt=True)
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
        # Replies routed through here re-send the keyboard so the /queue /go
        # buttons stay put; progress messages skip it to keep the chat quiet.
        await event.respond(text, buttons=KEYBOARD)

    async def _reply_long(self, event, text: str) -> None:
        # The compiled prompt can outrun Telegram's message cap; only the last
        # part carries the keyboard.
        parts = tgmd.chunks(text)
        for part in parts[:-1]:
            await event.respond(part)
        await self._reply(event, parts[-1])

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
        # The burst timer arms on receipt: a download can outlast
        # debounce_seconds and split one forward into N "batch of 1" messages.
        doc = getattr(msg, "document", None)
        doc_id = str(doc.id) if doc is not None else None
        dl_task = asyncio.create_task(self._download_media(event, doc_id))
        prev_commit = self._commit_chain.get(event.sender_id)
        task = asyncio.create_task(self._commit_media(event, dl_task, prev_commit))
        self._commit_chain[event.sender_id] = task
        self.accumulator.add(event.sender_id, (event, doc_id, task))

    async def _download_media(self, event, doc_id: str | None) -> tuple[str, str] | None:
        """Download the forward unless this exact file is already on disk.

        Returns (name, state) where state is "new" or "on disk", or None on
        failure. Does not touch the batch — see _commit_media for that.
        """
        msg = event.message
        known = self._media_names.get(doc_id) if doc_id is not None else None
        if known and (self.cfg.media_dir / known).exists():
            return known, "on disk"
        self.cfg.media_dir.mkdir(parents=True, exist_ok=True)
        # content.store hashes the file and keeps this suffix for the name.
        tmp = self.cfg.media_dir / f".tmp-{event.sender_id}-{msg.id}{msg.file.ext or ''}"
        try:
            await msg.download_media(file=str(tmp))
        except Exception as exc:  # a 2 GB download can fail mid-stream
            log.exception("download failed")
            tmp.unlink(missing_ok=True)
            await self._reply(event, f"Download failed: {exc}")
            return None
        # Telegram filenames are unusable; the on-disk name is the content
        # hash, so the same clip forwarded twice is one file.
        name, is_new = content.store(tmp, self.cfg.media_dir)
        state = "new" if is_new else "on disk"
        if doc_id is not None:
            self._media_names[doc_id] = name
            content.save_media_index(self.cfg.output_dir, self._media_names)
        return name, state

    async def _commit_media(
        self, event, dl_task: asyncio.Task, prev_commit: asyncio.Task | None
    ) -> tuple[MediaItem, str] | None:
        """Add a downloaded file to the batch, in arrival order.

        Downloads run concurrently and a small file can finish before a bigger
        one sent earlier, so insertion is serialized here behind the previous
        arrival's commit rather than behind its own download.
        """
        if prev_commit is not None:
            await asyncio.gather(prev_commit, return_exceptions=True)
        result = await dl_task
        if result is None:
            return None
        name, state = result
        return self._batch_for_new_item(event).add_media(name), state

    async def _announce_batch(self, sender_id, entries) -> None:
        """One message per forward burst: 'batch of N' + a line per distinct item.

        N is the batch size, not the forward count: content.store and add_media
        both collapse a repeat, so two identical files announce one item.
        """
        if not entries:
            return
        event = entries[-1][0]
        # The burst size and which files are already stored are known before any
        # byte moves; a fetch can run for minutes, so name it while it runs.
        pending = [
            e
            for e in entries
            if not (
                e[1] is not None
                and e[1] in self._media_names
                and (self.cfg.media_dir / self._media_names[e[1]]).exists()
            )
        ]
        if pending:
            await self._reply(event, f"Fetching {len(pending)} file(s) from Telegram…")
        results = await asyncio.gather(
            *(task for _, _, task in entries), return_exceptions=True
        )
        items, seen = [], set()
        for res in results:
            if isinstance(res, tuple):
                item, state = res
                if item.index not in seen:
                    seen.add(item.index)
                    items.append((item, state))
            elif isinstance(res, BaseException):
                log.error("store_media failed: %r", res)
            # else: None — the download failed and already told the user
        if not items:
            return
        lines = [f"batch of {len(items)} items detected"]
        lines += [
            f"[{n}/{len(items)}] {it.title or it.filename} — "
            f"{_size_prefix(self.cfg.media_dir, it.filename)}{state}"
            for n, (it, state) in enumerate(items, 1)
        ]
        await self._reply(event, "\n".join(lines))

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

    async def on_prompt(self, event) -> None:
        # Bare: show the exact system prompt a /go (or a post-summary correction)
        # would send — metaprompt + every prompt line + every correction.
        # "/prompt clear": drop those added lines but keep the transcripts, so a
        # bad instruction can be rewritten and re-summarised without a re-download.
        batch = self._batch(event)
        if event.raw_text.split()[1:2] == ["clear"]:
            batch.extra_prompt.clear()
            batch.corrections.clear()
            await self._reply(event, "Prompt cleared. Transcripts kept — add lines, then /go.")
            return
        await self._reply_long(
            event, build_system_prompt(self.cfg.metaprompt_path, batch)
        )

    def _batch_lines(self, batch: Batch) -> str:
        if not batch.media:
            return "Batch is empty."
        return "\n".join(
            f"[{m.index}] {m.filename} — "
            f"{_size_prefix(self.cfg.media_dir, m.filename)}"
            + ("transcribed" if m.transcript_path else "pending")
            for m in batch.media
        )

    async def on_text(self, event) -> None:
        text = event.raw_text.strip()
        links = text.split()
        # A message that is nothing but links is a list of transcription
        # sources, but only YouTube ones (run.sh feeds them to yt-dlp); a URL
        # inside a sentence stays a prompt/correction.
        if links and all(l.startswith(("http://", "https://")) for l in links):
            if len(links) > 1:
                await self._add_links(event, links)
                return
            kind = youtube.classify(text)
            if kind == "playlist":
                await self._expand_playlist(event, text)
                return
            if kind == "other":
                await self._reply(event, "Only YouTube links are supported.")
                return
            url = youtube.video_url(youtube.video_id(text))
            batch = self._batch_for_new_item(event)
            was = len(batch.media)
            item = batch.add_media(url)
            if len(batch.media) == was:
                await self._reply(event, f"[{item.index}] {url} — already in the batch.")
            else:
                await self._reply(event, f"[{item.index}] {url} — added\n/go to transcribe.")
            return
        # Any free-text message edits the prompt; nothing runs until /go. Before
        # the first summary the line is a prompt addition, after it a correction
        # — both feed build_system_prompt. The reply echoes only the additions,
        # not the metaprompt; /prompt shows the whole thing.
        batch = self._batch(event)
        batch.add_text(event.raw_text)
        run = "re-run" if batch.has_output else "run"
        await self._reply_long(
            event,
            f"Prompt updated — /go to {run} the summary, /prompt to see it in full."
            "\n\nOn top of the metaprompt:\n\n"
            + "\n\n".join([*batch.extra_prompt, *batch.corrections]),
        )

    async def _add_links(self, event, links: list[str]) -> None:
        """Several links in one message: every YouTube video becomes an item.

        Expanding a playlist waits on the worker, so it stays a one-link path;
        here it is named and skipped. The batch is only touched once at least
        one video is found, so a message of unusable links resets nothing.
        """
        kinds = [(link, youtube.classify(link)) for link in links]
        skipped = [
            f"{link} — skipped: "
            + ("send a playlist on its own" if kind == "playlist" else "not a YouTube link")
            for link, kind in kinds
            if kind != "video"
        ]
        videos = [link for link, kind in kinds if kind == "video"]
        if not videos:
            await self._reply(event, "\n".join(skipped))
            return
        batch = self._batch_for_new_item(event)
        added, seen = [], set()
        for link in videos:
            url = youtube.video_url(youtube.video_id(link))
            # Two link forms for one video are one item, so they are one line.
            if url in seen:
                continue
            seen.add(url)
            was = len(batch.media)
            item = batch.add_media(url)
            state = "added" if len(batch.media) != was else "already in the batch"
            added.append(f"[{item.index}] {url} — {state}")
        await self._reply(
            event,
            "\n".join(
                [f"batch of {len(added)} items detected", *added, *skipped, "/go to transcribe."]
            ),
        )

    async def _expand_playlist(self, event, url: str) -> None:
        # The container has no yt-dlp/cookies, so enumeration is a worker job.
        # The worker is serialized behind any running transcription, hence the
        # early reply and the bounded wait.
        await self._reply(event, "Expanding playlist…")
        job_id = new_job_id()
        write_expand_job(self.cfg.jobs_dir, job_id, url)
        try:
            result = await await_expand_result(
                self.cfg.jobs_dir,
                job_id,
                self.cfg.result_poll_seconds,
                self.cfg.expand_timeout_seconds,
            )
        except TimeoutError:
            await self._reply(event, "Playlist expansion timed out — nothing added.")
            return
        if result.get("error"):
            await self._reply(event, f"Playlist expansion failed: {result['error']}")
            return
        videos = result.get("videos") or []
        total = len(videos)
        videos = videos[: self.cfg.playlist_max]
        if not videos:
            await self._reply(event, "The playlist has no videos.")
            return
        batch = self._batch_for_new_item(event)
        added = [
            batch.add_media(youtube.video_url(v["id"]), title=v.get("title"))
            for v in videos
        ]
        lines = [f"batch of {len(added)} items detected"]
        if total > self.cfg.playlist_max:
            lines.append(f"cut from {total} to {self.cfg.playlist_max} (PLAYLIST_MAX)")
        lines += [
            f"[{n}/{len(added)}] {it.title or it.filename} — added"
            for n, it in enumerate(added, 1)
        ]
        await self._reply(event, "\n".join(lines))

    async def on_go(self, event) -> None:
        ahead = self.queue.qsize()
        await self.queue.put(lambda: self._go_job(event))
        if ahead:
            await self._reply(event, f"Queued behind {ahead} job(s).")

    async def on_reset(self, event) -> None:
        # ponytail: the job queue is shared by every sender; a whitelist bot is
        # one or two people, so /reset drops all queued work, not just this
        # sender's. Per-sender queues if that ever bites.
        dropped = 0
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            dropped += 1
        self._batch(event).reset()
        note = f" {dropped} queued job(s) dropped." if dropped else ""
        await self._reply(event, "Batch and prompt cleared." + note)

    # -- jobs -----------------------------------------------------------

    def _resolve_cached(self, batch: Batch) -> int:
        """Fill transcript_path for sources already transcribed on a past run.

        Keys are URLs or "<sha1><ext>" media names, both == MediaItem.filename.
        """
        cache = content.load_transcript_cache(self.cfg.output_dir)
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
                f"Transcribing {len(pending)} file(s) on the host worker…"
            )
            results = await await_result(
                self.cfg.jobs_dir, job_id, self.cfg.result_poll_seconds
            )
            batch.merge_results(results)
            lines = [
                f"[{r['index']}] {r['name']} — "
                f"{_size_prefix(self.cfg.media_dir, r['name'])}"
                + ("ok" if r.get("transcript_path") else f"failed: {r.get('error')}")
                for r in results
            ]
            await event.respond("Transcription:\n" + "\n".join(lines))

        if not batch.media:
            await event.respond("Batch is empty. Forward something first.")
            return
        if all(m.transcript_path is None for m in batch.media):
            await event.respond("Every file failed. /go to retry, /reset to clear.")
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
                f"Downloading the model {download['model']} "
                f"({_gb(download['total'])}) first, once — /status for progress…"
            )
        try:
            summary = await self._run_llm(system, text)
        except llm.LLMError as exc:
            self.downloading = False
            await self._send_document(event, _doc_name(batch, "transcripts"), text)
            await event.respond(
                f"Summary failed: {exc}\nSent the raw transcripts; re-send the correction to retry."
            )
            return
        self.downloading = False
        batch.has_output = True
        await self._send_document(event, _doc_name(batch, "summary"), summary)
        # The file is the artefact; the text is what gets read in the chat, so
        # send all of it — Telegram caps a message, hence the split.
        for part in tgmd.chunks(tgmd.to_html(summary)):
            await event.respond(part, parse_mode="html")

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
        (state.on_prompt, events.NewMessage(pattern=r"^/prompt(?:\s+clear)?$", func=mine)),
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
