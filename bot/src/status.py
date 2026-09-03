"""Runtime status, independent of Telegram: what a REST endpoint would return."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

_HF_API = "https://huggingface.co/api/models/{}?blobs=true"


def _repo_bytes(repo: str) -> int:
    """Download size of a Hugging Face repo; 0 when the API is unreachable."""
    try:
        with urllib.request.urlopen(_HF_API.format(repo), timeout=10) as response:
            data = json.load(response)
        return sum(f.get("size", 0) for f in data["siblings"])
    except Exception:
        return 0


def _dir_bytes(path: Path) -> int:
    # Symlinks are skipped: the cache points snapshots/ back at blobs/, so
    # counting both would double every file.
    return sum(
        f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()
    )


def model_download(models_dir: Path, model: str) -> dict | None:
    """How far the local model's download got, or None once it is complete.

    Blocking (one HTTP call) — call it off the event loop. An unreachable API
    reads as complete: a status line is not worth blocking a summary over.
    """
    if model.startswith((".", "/")):
        return None
    have = _dir_bytes(models_dir / f"models--{model.replace('/', '--')}")
    total = _repo_bytes(model)
    if not total or have >= total:
        return None
    return {"model": model, "bytes": have, "total": total}
