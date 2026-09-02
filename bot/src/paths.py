"""Translate the local telegram-bot-api server's file paths into spool paths."""

from __future__ import annotations

from pathlib import Path


def tg_path_to_relative(file_path: str, api_root: Path) -> str:
    """A path like `/var/lib/telegram-bot-api/<token>/videos/x.mp4` becomes
    `<token>/videos/x.mp4`, which resolves under tg-data on both sides.

    Falls back to the last three components if the prefix is not what we expect
    (local-mode path shape has varied across telegram-bot-api versions).
    """
    path = Path(file_path)
    try:
        return str(path.relative_to(api_root))
    except ValueError:
        if not path.is_absolute():
            return str(path)
        return str(Path(*path.parts[-3:]))
