import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from src import llm, status
from src.batch import Batch
from src.bot import BotApp
from tests.conftest import FakeEvent, FakeFile, FakeMessage


def _event(text="", file=None, msg_id=1, sender_id=1, content=b"fake", doc_id=None):
    return FakeEvent(
        FakeMessage(
            text=text, file=file, msg_id=msg_id, content=content, doc_id=doc_id
        ),
        sender_id=sender_id,
    )


def _batch(app, sender_id=1) -> Batch:
    return app.batches.setdefault(sender_id, Batch())


async def test_media_is_content_addressed_and_announced_once_per_burst(cfg):
    app = BotApp(cfg)
    e1 = _event(file=FakeFile(name="a.mp4"), msg_id=1, content=b"AAA")
    e2 = _event(file=FakeFile(name="b.mp4"), msg_id=2, content=b"BBB")
    await app.on_media(e1)
    await app.on_media(e2)
    await asyncio.sleep(0.05)  # cfg.debounce_seconds == 0.01

    saved = sorted(p.name for p in cfg.media_dir.glob("*.mp4"))
    names = [m.filename for m in app.batches[1].media]
    assert sorted(names) == saved
    assert all(len(n.split(".")[0]) == 40 for n in names)  # sha1 stems
    assert e1.responses == []  # only the last event in the burst answers
    assert e2.responses == [
        "Fetching 2 file(s) from Telegram…",
        f"batch of 2 items detected\n"
        f"[1/2] {names[0]} — 0.0 MB — new\n[2/2] {names[1]} — 0.0 MB — new",
    ]


async def test_batch_order_matches_arrival_not_download_speed(cfg):
    app = BotApp(cfg)
    e1 = _event(file=FakeFile(name="big.mp4"), msg_id=1, content=b"SLOW")  # sent first
    e2 = _event(file=FakeFile(name="small.mp4"), msg_id=2, content=b"FAST")  # sent second

    # e1 arrives first but its download finishes last — the race the fix covers.
    orig_download = e1.message.download_media

    async def slow_download(file):
        await asyncio.sleep(0.05)
        return await orig_download(file)

    e1.message.download_media = slow_download

    await app.on_media(e1)
    await app.on_media(e2)
    await asyncio.sleep(0.2)

    names = [m.filename for m in app.batches[1].media]
    assert names[0].startswith(hashlib.sha1(b"SLOW").hexdigest())
    assert names[1].startswith(hashlib.sha1(b"FAST").hexdigest())


async def test_duplicate_forward_announces_the_batch_size_not_the_forward_count(cfg):
    app = BotApp(cfg)
    events = [
        _event(file=FakeFile(name="a.mp4"), msg_id=1, content=b"SAME"),
        _event(file=FakeFile(name="b.mp4"), msg_id=2, content=b"SAME"),
        _event(file=FakeFile(name="c.mp4"), msg_id=3, content=b"OTHER"),
    ]
    for ev in events:
        await app.on_media(ev)
    await asyncio.sleep(0.05)

    assert len(list(cfg.media_dir.glob("*.mp4"))) == 2
    assert len(app.batches[1].media) == 2
    announce = events[-1].responses[-1]
    assert announce.startswith("batch of 2 items detected")
    assert announce.count("\n[") == 2


async def test_reforward_reuses_the_download_and_skips_the_ack(cfg):
    app = BotApp(cfg)
    e1 = _event(file=FakeFile(name="a.mp4"), msg_id=1, content=b"V", doc_id=555)
    await app.on_media(e1)
    await asyncio.sleep(0.05)
    name = app.batches[1].media[0].filename

    # Same Telegram document, second burst: no re-download, no "downloading…".
    e2 = _event(file=FakeFile(name="a.mp4"), msg_id=2, content=b"V", doc_id=555)
    await app.on_media(e2)
    await asyncio.sleep(0.05)

    assert e2.message.downloads == 0
    assert [m.filename for m in app.batches[1].media] == [name]
    assert e2.responses == ["batch of 1 items detected\n[1/1] " + name + " — 0.0 MB — on disk"]


async def test_media_index_survives_a_restart(cfg):
    app = BotApp(cfg)
    e1 = _event(file=FakeFile(name="a.mp4"), msg_id=1, content=b"V", doc_id=555)
    await app.on_media(e1)
    await asyncio.sleep(0.05)
    name = app.batches[1].media[0].filename

    # Fresh process: the index is reloaded from disk, so the re-forward of the
    # same Telegram document still skips the download.
    fresh = BotApp(cfg)
    e2 = _event(file=FakeFile(name="a.mp4"), msg_id=2, content=b"V", doc_id=555)
    await fresh.on_media(e2)
    await asyncio.sleep(0.05)

    assert e2.message.downloads == 0
    assert fresh.batches[1].media[0].filename == name
    assert e2.responses == ["batch of 1 items detected\n[1/1] " + name + " — 0.0 MB — on disk"]


async def test_forwarded_photo_is_skipped_by_mime(cfg):
    app = BotApp(cfg)
    ev = _event(file=FakeFile(name="p.jpg", ext=".jpg", mime_type="image/jpeg"), msg_id=73)
    await app.on_media(ev)

    assert not app.batches.get(1) or not app.batches[1].media
    assert not cfg.media_dir.exists() or not list(cfg.media_dir.iterdir())
    assert ev.responses == []


async def test_two_senders_get_separate_batches(cfg):
    app = BotApp(cfg)
    await app.on_media(
        _event(file=FakeFile(name="a.mp4"), msg_id=5, sender_id=2, content=b"S2")
    )
    await app.on_media(
        _event(file=FakeFile(name="b.mp4"), msg_id=6, sender_id=3, content=b"S3")
    )
    await asyncio.sleep(0.05)
    assert set(app.batches) == {2, 3}
    assert len(app.batches[2].media) == 1 and len(app.batches[3].media) == 1


async def test_forward_after_summary_starts_a_fresh_batch(cfg):
    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("old.mp4")
    b.has_output = True

    await app.on_media(_event(file=FakeFile(name="new.mp4"), msg_id=9, content=b"NEW"))
    await asyncio.sleep(0.05)
    assert len(app.batches[1].media) == 1
    assert app.batches[1].media[0].index == 1
    assert app.batches[1].media[0].filename.split(".")[0] != "old"


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


async def test_several_urls_in_one_message_each_become_a_media_item(cfg):
    app = BotApp(cfg)
    ev = _event(
        "https://www.youtube.com/watch?v=aaa\n"
        "https://youtu.be/bbb\n"
        "https://www.youtube.com/watch?v=aaa"
    )
    await app.on_text(ev)

    assert app.queue.empty()  # nothing runs until /go
    assert [m.filename for m in app.batches[1].media] == [
        "https://www.youtube.com/watch?v=aaa",
        "https://www.youtube.com/watch?v=bbb",
    ]
    assert ev.responses == [
        "batch of 2 items detected\n"
        "[1] https://www.youtube.com/watch?v=aaa — added\n"
        "[2] https://www.youtube.com/watch?v=bbb — added\n"
        "/go to transcribe."
    ]


async def test_unusable_links_among_several_are_named_not_swallowed(cfg):
    app = BotApp(cfg)
    ev = _event(
        "https://www.youtube.com/watch?v=aaa\n"
        "https://haraba.ru/Links/851c28\n"
        "https://www.youtube.com/playlist?list=PLabc"
    )
    await app.on_text(ev)

    assert [m.filename for m in app.batches[1].media] == [
        "https://www.youtube.com/watch?v=aaa"
    ]
    assert not app.batches[1].extra_prompt  # not filed away as prompt text
    assert ev.responses == [
        "batch of 1 items detected\n"
        "[1] https://www.youtube.com/watch?v=aaa — added\n"
        "https://haraba.ru/Links/851c28 — skipped: not a YouTube link\n"
        "https://www.youtube.com/playlist?list=PLabc — skipped: send a playlist on its own\n"
        "/go to transcribe."
    ]


async def test_a_message_of_only_unusable_links_leaves_the_batch_alone(cfg):
    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("old.mp4")
    b.has_output = True

    ev = _event("https://haraba.ru/a\nhttps://haraba.ru/b")
    await app.on_text(ev)

    assert [m.filename for m in b.media] == ["old.mp4"]  # no reset, no additions
    assert b.has_output is True
    assert ev.responses == [
        "https://haraba.ru/a — skipped: not a YouTube link\n"
        "https://haraba.ru/b — skipped: not a YouTube link"
    ]


async def test_cached_youtube_transcript_skips_the_worker(cfg, monkeypatch):
    (cfg.output_dir / "vid").mkdir(parents=True)
    (cfg.output_dir / "vid" / "vid.txt").write_text("[SPEAKER_00]: hi")
    (cfg.output_dir / "transcript-cache.json").write_text(
        '{"https://www.youtube.com/watch?v=abc": "vid/vid.txt"}'
    )
    monkeypatch.setattr(llm, "run_prompt", lambda *a, **k: "SUMMARY")

    app = BotApp(cfg)
    _batch(app).add_media("https://www.youtube.com/watch?v=abc")
    ev = _event()
    await app._go_job(ev)

    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())
    assert app.batches[1].media[0].transcript_path == "vid/vid.txt"
    assert ev.client.files == [(42, "vid.summary.md")]


async def test_instruction_after_summary_waits_for_go_and_survives_a_new_link(
    cfg, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(
        llm, "run_prompt", lambda system, text, **kw: calls.append((system, text)) or "S"
    )

    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("old.mp4")
    b.media[0].transcript_path = "old/old.txt"
    b.has_output = True

    await app.on_text(_event("просто перескажи видео"))  # correction, not run
    assert app.queue.empty()
    assert calls == []
    assert b.corrections == ["просто перескажи видео"]

    await app.on_text(_event("https://youtu.be/NEW"))  # new video, fresh batch

    nb = app.batches[1]
    assert [m.filename for m in nb.media] == ["https://www.youtube.com/watch?v=NEW"]
    assert nb.extra_prompt == ["просто перескажи видео"]  # carried onto the new batch
    assert nb.has_output is False
    assert calls == []


async def test_url_inside_a_sentence_stays_a_correction(cfg):
    app = BotApp(cfg)
    _batch(app).has_output = True
    ev = _event("fix the link to https://x.com/y")
    await app.on_text(ev)
    assert app.queue.empty()  # nothing runs until /go
    assert app.batches[1].corrections == ["fix the link to https://x.com/y"]
    assert not app.batches[1].media


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
    assert len(ev.responses) == 1
    assert "/go" in ev.responses[0]
    assert "use bullet points" in ev.responses[0]
    assert "BASE PROMPT" not in ev.responses[0]  # metaprompt not echoed on every add
    assert app.batches[1].extra_prompt == ["use bullet points"]
    assert app.queue.empty()
    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())


async def test_prompt_command_shows_the_compiled_system_prompt(cfg):
    app = BotApp(cfg)
    b = _batch(app)
    b.add_text("focus on the compressor pedal")
    b.has_output = True
    b.add_text("keep it terse")
    ev = _event()
    await app.on_prompt(ev)
    assert ev.responses == [
        "BASE PROMPT\n\nfocus on the compressor pedal\n\nkeep it terse"
    ]


async def test_prompt_clear_drops_added_lines_but_keeps_the_transcripts(cfg):
    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("1.mp4")
    b.media[0].transcript_path = "1/1.txt"
    b.add_text("first pass instruction")
    b.has_output = True
    b.add_text("bad correction")

    ev = _event("/prompt clear")
    await app.on_prompt(ev)

    assert b.extra_prompt == [] and b.corrections == []
    assert [m.transcript_path for m in b.media] == ["1/1.txt"]
    assert b.has_output is True
    assert "cleared" in ev.responses[0].lower()


async def test_reset_drops_queued_jobs_and_clears_the_prompt(cfg):
    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("1.mp4")
    b.add_text("x")
    b.has_output = True
    await app.queue.put(lambda: None)
    await app.queue.put(lambda: None)

    ev = _event()
    await app.on_reset(ev)

    assert app.queue.empty()
    assert b.media == [] and b.extra_prompt == [] and b.corrections == []
    assert b.has_output is False
    assert ev.responses == ["Batch and prompt cleared. 2 queued job(s) dropped."]


async def test_correction_applies_on_the_next_go_stage2_only_no_stage1(cfg, monkeypatch):
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

    await app.on_text(_event("fix the intro"))
    assert app.queue.empty()  # correction staged, not run

    go = _event()
    await app._go_job(go)

    assert calls["text"] == "=== [1] 1_1_x.mp4 ===\n[SPEAKER_00]: hi"
    assert "fix the intro" in calls["system"]
    assert go.client.files == [(42, "1.summary.md")]
    assert b.has_output is True
    assert not cfg.jobs_dir.exists() or not list(cfg.jobs_dir.iterdir())


async def test_local_provider_hands_stage2_to_the_worker(cfg, monkeypatch):
    (cfg.output_dir / "1").mkdir(parents=True)
    (cfg.output_dir / "1" / "1.txt").write_text("[SPEAKER_00]: hi")
    _model(cfg, "mlx-community/X", have=100, total=100, monkeypatch=monkeypatch)
    app = BotApp(replace(cfg, llm_provider="local", llm_model="mlx-community/X"))
    b = _batch(app)
    b.add_media("1_1_x.mp4")
    b.media[0].transcript_path = "1/1.txt"

    job = {}

    async def fake_worker():
        while not (jobs := list(cfg.jobs_dir.glob("*.llm.json"))):
            await asyncio.sleep(0.01)
        job.update(json.loads(jobs[0].read_text()))
        result = cfg.jobs_dir / jobs[0].name.replace(".llm.json", ".result.json")
        result.write_text(json.dumps({"summary": "<think>hmm</think>SUMMARY"}))
        jobs[0].unlink()

    worker = asyncio.create_task(fake_worker())
    ev = _event()
    await app._stage2_job(ev)
    await worker

    assert job["model"] == "mlx-community/X"
    assert job["thinking"] is False
    assert job["text"] == "=== [1] 1_1_x.mp4 ===\n[SPEAKER_00]: hi"
    assert "BASE PROMPT" in job["system"]
    assert ev.responses == ["SUMMARY"]  # the reasoning block is stripped
    assert not list(cfg.jobs_dir.iterdir())


def _model(cfg, repo, have, total, monkeypatch):
    """Fake an HF cache holding `have` of the repo's `total` bytes."""
    cached = cfg.models_dir / f"models--{repo.replace('/', '--')}"
    cached.mkdir(parents=True)
    (cached / "weights").write_bytes(b"x" * have)
    monkeypatch.setattr(status, "_repo_bytes", lambda _: total)


async def test_uncached_local_model_reports_its_size_then_the_progress(cfg, monkeypatch):
    (cfg.output_dir / "1").mkdir(parents=True)
    (cfg.output_dir / "1" / "1.txt").write_text("[SPEAKER_00]: hi")
    _model(cfg, "mlx-community/X", have=3_000_000_000, total=4_000_000_000,
           monkeypatch=monkeypatch)
    app = BotApp(replace(cfg, llm_provider="local", llm_model="mlx-community/X"))
    b = _batch(app)
    b.add_media("1_1_x.mp4")
    b.media[0].transcript_path = "1/1.txt"

    status_ev = _event()

    async def fake_worker():
        while not (jobs := list(cfg.jobs_dir.glob("*.llm.json"))):
            await asyncio.sleep(0.01)
        # /status is answered while the summary job is still out with the worker.
        await app.on_status(status_ev)
        result = cfg.jobs_dir / jobs[0].name.replace(".llm.json", ".result.json")
        result.write_text(json.dumps({"summary": "SUMMARY"}))
        jobs[0].unlink()

    worker = asyncio.create_task(fake_worker())
    ev = _event()
    await app._stage2_job(ev)
    await worker

    assert "Downloading the model" in ev.responses[0] and "(4.0 GB)" in ev.responses[0]
    assert ev.responses[1] == "SUMMARY"
    assert "mlx-community/X: 3.0 GB of 4.0 GB (75%)" in status_ev.responses[0]


async def test_status_stays_quiet_about_a_model_already_on_disk(cfg, monkeypatch):
    _model(cfg, "mlx-community/X", have=100, total=100, monkeypatch=monkeypatch)
    app = BotApp(replace(cfg, llm_provider="local", llm_model="mlx-community/X"))
    app.downloading = True
    ev = _event()
    await app.on_status(ev)

    assert ev.responses == ["Queue: 0 job(s) waiting."]


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

    assert ev.client.files == [(42, "1.transcripts.md")]
    assert any("Summary failed" in r for r in ev.responses)
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
    assert ev.client.files == [(42, "a.summary.md")]


# -- playlist expansion --------------------------------------------------


async def _answer_expand(cfg, videos, error=None):
    """Stand in for the worker: answer the first .expand.json job that appears."""
    for _ in range(200):
        jobs = list(cfg.jobs_dir.glob("*.expand.json")) if cfg.jobs_dir.exists() else []
        if jobs:
            jid = jobs[0].name.removesuffix(".expand.json")
            (cfg.jobs_dir / f"{jid}.result.json").write_text(
                json.dumps({"videos": videos, "error": error})
            )
            jobs[0].unlink()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no expand job appeared")


async def test_playlist_expands_into_titled_watch_urls_in_order(cfg):
    app = BotApp(cfg)
    ev = _event("https://www.youtube.com/playlist?list=PLabc")
    await asyncio.gather(
        app.on_text(ev),
        _answer_expand(cfg, [{"id": "a", "title": "First"}, {"id": "b", "title": "Second"}]),
    )

    b = app.batches[1]
    assert [m.filename for m in b.media] == [
        "https://www.youtube.com/watch?v=a",
        "https://www.youtube.com/watch?v=b",
    ]
    assert [m.title for m in b.media] == ["First", "Second"]
    assert ev.responses[0] == "Expanding playlist…"
    assert ev.responses[-1] == (
        "batch of 2 items detected\n[1/2] First — added\n[2/2] Second — added"
    )


async def test_playlist_longer_than_max_is_cut_with_a_note(cfg):
    app = BotApp(replace(cfg, playlist_max=2))
    ev = _event("https://www.youtube.com/playlist?list=PLabc")
    videos = [{"id": f"v{i}", "title": f"T{i}"} for i in range(5)]
    await asyncio.gather(app.on_text(ev), _answer_expand(cfg, videos))

    assert [m.filename for m in app.batches[1].media] == [
        "https://www.youtube.com/watch?v=v0",
        "https://www.youtube.com/watch?v=v1",
    ]
    assert "cut from 5 to 2 (PLAYLIST_MAX)" in ev.responses[-1]


async def test_playlist_expand_error_surfaces_and_adds_nothing(cfg):
    app = BotApp(cfg)
    ev = _event("https://www.youtube.com/playlist?list=PLabc")
    await asyncio.gather(
        app.on_text(ev), _answer_expand(cfg, [], error="Private playlist")
    )

    assert not app.batches.get(1) or not app.batches[1].media
    assert any("Private playlist" in r for r in ev.responses)


async def test_playlist_expand_timeout_adds_nothing(cfg):
    app = BotApp(replace(cfg, expand_timeout_seconds=0.05))
    ev = _event("https://www.youtube.com/playlist?list=PLabc")
    await app.on_text(ev)  # nothing answers the job

    assert not app.batches.get(1) or not app.batches[1].media
    assert any("timed out" in r for r in ev.responses)


async def test_resolve_cached_fills_transcript_path_from_a_media_hash_key(cfg):
    (cfg.output_dir / "h").mkdir(parents=True)
    (cfg.output_dir / "h" / "h.txt").write_text("x")
    (cfg.output_dir / "transcript-cache.json").write_text('{"deadbeef.mp3": "h/h.txt"}')

    app = BotApp(cfg)
    b = _batch(app)
    b.add_media("deadbeef.mp3")
    assert app._resolve_cached(b) == 1
    assert b.media[0].transcript_path == "h/h.txt"
