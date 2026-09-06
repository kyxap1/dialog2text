import pytest

from src.core import youtube


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://www.youtube.com/watch?v=abc", "video"),
        ("https://youtu.be/abc", "video"),
        ("https://youtube.com/watch?v=abc&t=30", "video"),
        ("https://www.youtube.com/playlist?list=PLabc", "playlist"),
        ("https://www.youtube.com/watch?v=abc&list=PLabc", "playlist"),
        ("https://www.youtube.com/watch?v=abc&list=RDabc", "video"),
        ("https://www.youtube.com/playlist?list=RDabc", "other"),
        ("https://haraba.ru/Links/851c28", "other"),
    ],
)
def test_classify(url, kind):
    assert youtube.classify(url) == kind


def test_video_id():
    assert youtube.video_id("https://youtu.be/xY9?si=1") == "xY9"
    assert youtube.video_id("https://m.youtube.com/watch?v=abc&t=1") == "abc"
    assert youtube.video_id("https://haraba.ru/x") is None


def test_playlist_id():
    assert youtube.playlist_id("https://www.youtube.com/watch?v=a&list=PLx") == "PLx"
    assert youtube.playlist_id("https://youtu.be/a") is None
    assert youtube.playlist_id("https://haraba.ru/x") is None


def test_video_url_is_canonical():
    assert youtube.video_url("abc") == "https://www.youtube.com/watch?v=abc"
