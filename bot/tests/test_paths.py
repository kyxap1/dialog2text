from pathlib import Path

from src.paths import tg_path_to_relative

ROOT = Path("/var/lib/telegram-bot-api")


def test_strips_the_api_root_prefix():
    p = "/var/lib/telegram-bot-api/123abc/videos/file_5.mp4"
    assert tg_path_to_relative(p, ROOT) == "123abc/videos/file_5.mp4"


def test_unexpected_absolute_prefix_falls_back_to_last_three_parts():
    p = "/srv/telegram/data/123abc/videos/file_5.mp4"
    assert tg_path_to_relative(p, ROOT) == "123abc/videos/file_5.mp4"


def test_file_uri_prefix_is_stripped():
    p = "file:///var/lib/telegram-bot-api/123abc/videos/file_5.mp4"
    assert tg_path_to_relative(p, ROOT) == "123abc/videos/file_5.mp4"


def test_already_relative_path_is_returned_as_is():
    assert tg_path_to_relative("123abc/videos/file_5.mp4", ROOT) == "123abc/videos/file_5.mp4"
