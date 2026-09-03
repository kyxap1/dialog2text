"""YouTube URL classification, id parsing, and canonical watch URLs.

Pure string work: no network, no Telethon, no Config.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import ParseResult, parse_qs, urlparse

Kind = Literal["video", "playlist", "other"]


def _host(url: str) -> tuple[ParseResult, str]:
    u = urlparse(url)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    return u, host


def _is_youtube(host: str) -> bool:
    return host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com")


def video_id(url: str) -> str | None:
    """Video id for a watch or youtu.be link, else None."""
    u, host = _host(url)
    if host == "youtu.be":
        return u.path.lstrip("/").split("/")[0] or None
    if _is_youtube(host):
        return (parse_qs(u.query).get("v") or [None])[0]
    return None


def playlist_id(url: str) -> str | None:
    u, host = _host(url)
    if not _is_youtube(host):
        return None
    return (parse_qs(u.query).get("list") or [None])[0]


def classify(url: str) -> Kind:
    lst = playlist_id(url)
    # list=RD… is a radio/mix: generated and effectively endless, never expanded.
    if lst and not lst.startswith("RD"):
        return "playlist"
    if video_id(url):
        return "video"
    return "other"


def video_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"
