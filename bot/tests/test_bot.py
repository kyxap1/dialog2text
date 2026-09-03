import asyncio

import pytest

from src import llm
from src.batch import Batch
from src.bot import BotApp
from tests.conftest import FakeEvent, FakeFile, FakeMessage


def _event(text="", file=None, msg_id=1, sender_id=1):
    return FakeEvent(FakeMessage(text=text, file=file, msg_id=msg_id), sender_id=sender_id)


def _batch(app, sender_id=1) -> Batch:
    return app.batches.setdefault(sender_id, Batch())


async def test_media_saved_under_sender_msg_hash_name(cfg):
    app = BotApp(cfg)
    ev = _event(file=FakeFile(name="clip.mp4"), msg_id=77)
    await app.on_media(ev)

    saved = list(cfg.media_dir.glob("1_77_*.mp4"))
    assert len(saved) == 1
    assert app.batches[1].media[0].filename == saved[0].name
    assert ev.responses == [f"Added [1] {saved[0].name}"]


async def test_forwarded_photo_is_skipped_by_mime(cfg):
    app = BotApp(cfg)
    ev = _event(file=FakeFile(name="p.jpg", ext=".jpg", mime_type="image/jpeg"), msg_id=73)
    await app.on_media(ev)

    assert not app.batches.get(1) or not app.batches[1].media
    assert not cfg.media_dir.exists() or not list(cfg.media_dir.iterdir())
    assert ev.responses == []


async def test_two_senders_get_separate_batches(cfg):
    app = BotApp(cfg)
    await app.on_media(_event(file=FakeFile(name="a.mp4"), msg_id=5, sender_id=2))
    await app.on_media(_event(file=FakeFile(name="b.mp4"), msg_id=6, sender_id=3))
    assert app.batches[2].media[0].filename.startswith("2_5_")
    assert app.batches[3].media[0].filename.startswith("3_6_")


async def test_forward_after_summary_starts_a_fresh_batch(cfg):
    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("old.mp4")
    b.has_output = True

    await app.on_media(_event(file=FakeFile(name="new.mp4"), msg_id=9))
    assert len(app.batches[1].media) == 1
    assert app.batches[1].media[0].index == 1
    assert app.batches[1].media[0].filename.startswith("1_9_")


async def test_bare_url_is_added_as_a_canonical_media_item_not_a_correction(cfg):
    app = BotApp(cfg)
    _batch(app).has_output = True
    ev = _event("https://youtube.com/watch?v=abc&t=30")
    await app.on_text(ev)
    assert app.queue.empty()  # nothing runs until /go
    b = app.batches[1]
    assert [m.filename for m in b.media] == ["https://www.youtube.com/watch?v=abc"]
    assert b.has_output is False  # started a fresh batch


async def test_same_video_via_two_link_forms_is_one_item(cfg):
    app = BotApp(cfg)
    await app.on_text(_event("https://www.youtube.com/watch?v=abc"))
    dupe = _event("https://youtu.be/abc")
    await app.on_text(dupe)
    assert [m.filename for m in app.batches[1].media] == [
        "https://www.youtube.com/watch?v=abc"
    ]
    assert "already in the batch" in dupe.responses[0]


async def test_cached_youtube_transcript_skips_the_worker(cfg, monkeypatch):
    (cfg.output_dir / "vid").mkdir(parents=True)
    (cfg.output_dir / "vid" / "vid.txt").write_text("[SPEAKER_00]: hi")
    (cfg.output_dir / "url-cache.json").write_text(
        '{"https://www.youtube.com/watch?v=abc": "vid/vid.txt"}'
    )
    monkeypatch.setattr(llm, "run_prompt", lambda *a, **k: "SUMMARY")

    app = BotApp(cfg)
    _batch(app).add_media("https://www.youtube.com/watch?v=abc")
    ev = _event()
    await app._go_job(ev)

    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())
    assert app.batches[1].media[0].transcript_path == "vid/vid.txt"
    assert ev.client.files == [(42, "summary.md")]


async def test_url_inside_a_sentence_stays_a_correction(cfg):
    app = BotApp(cfg)
    _batch(app).has_output = True
    ev = _event("fix the link to https://x.com/y")
    await app.on_text(ev)
    assert app.queue.qsize() == 1


async def test_non_youtube_url_is_not_added_and_not_a_correction(cfg):
    app = BotApp(cfg)
    _batch(app).has_output = True
    ev = _event("https://haraba.ru/Links/851c28")
    await app.on_text(ev)
    assert app.queue.empty()
    assert not app.batches[1].media
    assert "YouTube" in ev.responses[0]


async def test_queue_lists_media_with_transcription_state(cfg):
    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("1_1_aaa.mp4")
    b.add_media("1_2_bbb.mp4")
    b.media[0].transcript_path = "1_1_aaa/1_1_aaa.txt"

    ev = _event()
    await app.on_queue(ev)
    assert ev.responses == ["[1] 1_1_aaa.mp4 — transcribed\n[2] 1_2_bbb.mp4 — pending"]


async def test_text_before_output_is_a_prompt_addition_and_queues_nothing(cfg):
    app = BotApp(cfg)
    ev = _event("use bullet points")
    await app.on_text(ev)
    assert ev.responses == ["Added to the prompt."]
    assert app.queue.empty()
    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())


async def test_correction_reruns_stage2_only_no_job_file_no_stage1(cfg, monkeypatch):
    (cfg.output_dir / "1").mkdir(parents=True)
    (cfg.output_dir / "1" / "1.txt").write_text("[SPEAKER_00]: hi")

    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("1_1_x.mp4")
    b.media[0].transcript_path = "1/1.txt"
    b.has_output = True

    calls = {}
    monkeypatch.setattr(
        llm, "run_prompt", lambda system, text, **kw: calls.update(system=system, text=text) or "SUMMARY"
    )

    ev = _event("fix the intro")
    await app.on_text(ev)
    assert app.queue.qsize() == 1

    job = await app.queue.get()
    await job()

    assert calls["text"] == "=== [1] 1_1_x.mp4 ===\n[SPEAKER_00]: hi"
    assert "fix the intro" in calls["system"]
    assert ev.client.files == [(42, "summary.md")]
    assert b.has_output is True
    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())


async def test_llm_error_sends_raw_transcripts_and_keeps_batch(cfg, monkeypatch):
    (cfg.output_dir / "1").mkdir(parents=True)
    (cfg.output_dir / "1" / "1.txt").write_text("raw text")
    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("1_1_x.mp4")
    b.media[0].transcript_path = "1/1.txt"

    def boom(*a, **k):
        raise llm.LLMError("429 rate limited")

    monkeypatch.setattr(llm, "run_prompt", boom)
    ev = _event()
    await app._stage2_job(ev)

    assert ev.client.files == [(42, "transcripts.md")]
    assert any("LLM error" in r for r in ev.responses)
    assert app.batches[1].media  # batch kept


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
    b = _batch(app)
    b.add_media("a.mp4")
    b.add_media("b.mp4")
    b.media[0].transcript_path = "a/a.txt"  # already done

    monkeypatch.setattr(llm, "run_prompt", lambda *a, **k: "SUMMARY")

    (cfg.output_dir / "a").mkdir(parents=True)
    (cfg.output_dir / "a" / "a.txt").write_text("done")
    (cfg.output_dir / "b").mkdir(parents=True)
    (cfg.output_dir / "b" / "b.txt").write_text("new")

    ev = _event()

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

    await asyncio.gather(app._go_job(ev), fake_worker())

    assert app.batches[1].media[1].transcript_path == "b/b.txt"
    assert ev.client.files == [(42, "summary.md")]
