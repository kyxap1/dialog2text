"""Content-addressed media storage and the shared transcript cache.

Pure over paths: no Telethon, no Config.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# Holds both URL keys and "<sha1><ext>" media keys; written by the host worker.
TRANSCRIPT_CACHE_NAME = "transcript-cache.json"


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_name(path: Path) -> str:
    """On-disk name for a file: its SHA-1 plus the original suffix."""
    return f"{_sha1(path)}{path.suffix}"


def store(tmp: Path, media_dir: Path) -> tuple[str, bool]:
    """Move `tmp` into `media_dir` under its content-addressed name.

    Returns (name, is_new). If the name already exists it is a dedup hit:
    `tmp` is unlinked and is_new is False.
    """
    name = content_name(tmp)
    dest = media_dir / name
    if dest.exists():
        tmp.unlink(missing_ok=True)
        return name, False
    media_dir.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)
    return name, True


def load_transcript_cache(output_dir: Path) -> dict[str, str]:
    try:
        return json.loads((output_dir / TRANSCRIPT_CACHE_NAME).read_text())
    except (OSError, ValueError):
        return {}
