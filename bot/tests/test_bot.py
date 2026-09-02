import asyncio

import pytest

from src import llm
from src.bot import BotApp
from tests.conftest import FakeBot, FakeContext, FakeMessage, FakeUpdate


def _update(text=None):
    return FakeUpdate(FakeMessage(text=text))


async def test_text_before_output_is_a_prompt_addition_and_queues_nothing(cfg):
    app = BotApp(cfg)
    upd = _update("use bullet points")
    await app.on_text(upd, FakeContext(FakeBot()))
    assert upd.message.replies == ["Added to the prompt."]
    assert app.queue.empty()
    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())


async def test_correction_reruns_stage2_only_no_job_file_no_stage1(cfg, monkeypatch):
    (cfg.output_dir / "1").mkdir(parents=True)
    (cfg.output_dir / "1" / "1.txt").write_text("[SPEAKER_00]: hi")

    app = BotApp(cfg)
    app.batch.add_media("1.mp4")
    app.batch.media[0].transcript_path = "1/1.txt"
    app.batch.has_output = True

    calls = {}
    monkeypatch.setattr(
        llm, "run_prompt", lambda system, text, **kw: calls.update(system=system, text=text) or "SUMMARY"
    )

    upd = _update("fix the intro")
    ctx = FakeContext(FakeBot())
    await app.on_text(upd, ctx)
    assert app.queue.qsize() == 1

    job = await app.queue.get()
    await job()

    assert calls["text"] == "=== [1] 1 ===\n[SPEAKER_00]: hi"
    assert "fix the intro" in calls["system"]
    assert ctx.bot.documents == [(42, "summary.md")]
    assert app.batch.has_output is True
    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())


async def test_llm_error_sends_raw_transcripts_and_keeps_batch(cfg, monkeypatch):
    (cfg.output_dir / "1").mkdir(parents=True)
    (cfg.output_dir / "1" / "1.txt").write_text("raw text")
    app = BotApp(cfg)
    app.batch.add_media("1.mp4")
    app.batch.media[0].transcript_path = "1/1.txt"

    def boom(*a, **k):
        raise llm.LLMError("429 rate limited")

    monkeypatch.setattr(llm, "run_prompt", boom)
    upd = _update()
    ctx = FakeContext(FakeBot())
    await app._stage2_job(upd, ctx)

    assert ctx.bot.documents == [(42, "transcripts.md")]
    assert any("LLM error" in r for r in upd.message.replies)
    assert app.batch.media  # batch kept


async def test_queue_runs_jobs_in_fifo_order(cfg):
    app = BotApp(cfg)
    order = []
    loop_task = asyncio.create_task(app._run_queue())

    for n in (1, 2, 3):
        await app.queue.put(lambda n=n: _record(order, n))
    await app.queue.join()
    loop_task.cancel()
    assert order == [1, 2, 3]


async def _record(order, n):
    order.append(n)


async def test_go_writes_a_job_for_untranscribed_items_then_runs_stage2(cfg, monkeypatch):
    app = BotApp(cfg)
    app.batch.add_media("a.mp4")
    app.batch.add_media("b.mp4")
    app.batch.media[0].transcript_path = "a/a.txt"  # already done

    monkeypatch.setattr(llm, "run_prompt", lambda *a, **k: "SUMMARY")

    (cfg.output_dir / "a").mkdir(parents=True)
    (cfg.output_dir / "a" / "a.txt").write_text("done")
    (cfg.output_dir / "b").mkdir(parents=True)
    (cfg.output_dir / "b" / "b.txt").write_text("new")

    upd = _update()
    ctx = FakeContext(FakeBot())

    async def fake_worker():
        # stand in for the host worker: wait for the job, answer it
        for _ in range(200):
            jobs = list(cfg.jobs_dir.glob("*.job.json")) if cfg.jobs_dir.exists() else []
            if jobs:
                import json

                media = json.loads(jobs[0].read_text())["media"]
                assert media == ["b.mp4"], media
                jid = jobs[0].name.removesuffix(".job.json")
                (cfg.jobs_dir / f"{jid}.result.json").write_text(
                    json.dumps({"results": [{"index": 2, "name": "b.mp4", "transcript_path": "b/b.txt", "error": None}]})
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no job file appeared")

    await asyncio.gather(app._go_job(upd, ctx), fake_worker())

    assert app.batch.media[1].transcript_path == "b/b.txt"
    assert ctx.bot.documents == [(42, "summary.md")]
