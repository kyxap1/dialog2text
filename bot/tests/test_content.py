from src.core import content


def test_identical_bytes_get_identical_name(tmp_path):
    a = tmp_path / "a.mp3"
    a.write_bytes(b"hello world")
    b = tmp_path / "b.ogg"
    b.write_bytes(b"hello world")
    # Same content, so the same hash; the suffix is the file's own.
    assert content.content_name(a).split(".")[0] == content.content_name(b).split(".")[0]
    assert content.content_name(a).endswith(".mp3")


def test_store_moves_new_content_and_reports_is_new(tmp_path):
    media = tmp_path / "media"
    tmp = tmp_path / ".incoming.mp3"
    tmp.write_bytes(b"abc")
    name, is_new = content.store(tmp, media)
    assert is_new is True
    assert (media / name).read_bytes() == b"abc"
    assert not tmp.exists()


def test_store_dedups_identical_content_without_rewriting(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    first = tmp_path / ".one.mp3"
    first.write_bytes(b"same")
    name1, _ = content.store(first, media)
    stored = media / name1
    mtime = stored.stat().st_mtime_ns

    second = tmp_path / ".two.mp3"
    second.write_bytes(b"same")
    name2, is_new = content.store(second, media)
    assert name2 == name1
    assert is_new is False
    assert not second.exists()
    assert stored.stat().st_mtime_ns == mtime  # untouched keeps FIFO honest


def test_different_content_gets_a_different_name(tmp_path):
    media = tmp_path / "media"
    one = tmp_path / ".a.mp3"
    one.write_bytes(b"aaa")
    two = tmp_path / ".b.mp3"
    two.write_bytes(b"bbb")
    n1, _ = content.store(one, media)
    n2, _ = content.store(two, media)
    assert n1 != n2


def test_load_transcript_cache_missing_is_empty(tmp_path):
    assert content.load_transcript_cache(tmp_path) == {}


def test_load_transcript_cache_reads_both_key_kinds(tmp_path):
    (tmp_path / "transcript-cache.json").write_text(
        '{"https://www.youtube.com/watch?v=a": "a/a.txt", "deadbeef.mp3": "d/d.txt"}'
    )
    assert content.load_transcript_cache(tmp_path) == {
        "https://www.youtube.com/watch?v=a": "a/a.txt",
        "deadbeef.mp3": "d/d.txt",
    }
