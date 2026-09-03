import json

from src.spool import await_result, new_job_id, write_job


def test_new_job_id_is_unique_and_prefixed_by_utc_timestamp():
    a, b = new_job_id(), new_job_id()
    assert a != b
    assert a[:8].isdigit() and a[8] == "T"


def test_write_job_lists_exactly_the_given_media():
    import tempfile
    from pathlib import Path

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
