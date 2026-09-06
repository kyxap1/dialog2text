import json
import tempfile
from pathlib import Path

import pytest

from src.spool import (
    await_expand_result,
    await_result,
    new_job_id,
    write_expand_job,
    write_job,
)


def test_new_job_id_is_unique_and_prefixed_by_utc_timestamp():
    a, b = new_job_id(), new_job_id()
    assert a != b
    assert a[:8].isdigit() and a[8] == "T"


def test_write_job_lists_exactly_the_given_media():
    with tempfile.TemporaryDirectory() as d:
        jobs = Path(d)
        write_job(jobs, "20260101T000000-abcd", ["tok/videos/2.mp4"])
        files = list(jobs.glob("*.job.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text()) == {"media": ["tok/videos/2.mp4"]}
        assert not list(jobs.glob(".*tmp"))  # temp file renamed away


async def test_await_result_returns_results_and_removes_the_file(tmp_path):
    payload = {"results": [{"index": 1, "name": "a.mp4", "transcript_path": "a/a.txt", "error": None}]}
    (tmp_path / "j.result.json").write_text(json.dumps(payload))
    results = await await_result(tmp_path, "j", poll_seconds=0.01)
    assert results == payload["results"]
    assert not (tmp_path / "j.result.json").exists()


def test_write_expand_job_writes_just_the_url():
    with tempfile.TemporaryDirectory() as d:
        jobs = Path(d)
        write_expand_job(jobs, "20260101T000000-abcd", "https://youtube.com/playlist?list=PL")
        files = list(jobs.glob("*.expand.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text()) == {
            "url": "https://youtube.com/playlist?list=PL"
        }


async def test_await_expand_result_returns_payload_and_removes_the_file(tmp_path):
    payload = {"videos": [{"id": "a", "title": "A"}], "error": None}
    (tmp_path / "j.result.json").write_text(json.dumps(payload))
    got = await await_expand_result(tmp_path, "j", poll_seconds=0.01, timeout=1.0)
    assert got == payload
    assert not (tmp_path / "j.result.json").exists()


async def test_await_expand_result_times_out(tmp_path):
    with pytest.raises(TimeoutError):
        await await_expand_result(tmp_path, "j", poll_seconds=0.01, timeout=0.03)
