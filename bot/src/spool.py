"""Filesystem spool between the bot container and the host worker.

Single writer on each side: the bot writes *.job.json, the worker writes
*.result.json. See the design doc's "Spool protocol" section.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def new_job_id() -> str:
    # Timestamp prefix so the worker's lexical "oldest first" pick is FIFO.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def write_job(jobs_dir: Path, job_id: str, media: list[str]) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job_id}.job.json"
    tmp = jobs_dir / f".{job_id}.job.json.tmp"
    tmp.write_text(json.dumps({"media": media}))
    os.replace(tmp, path)  # atomic: the worker never sees a half-written job
    return path


async def await_result(
    jobs_dir: Path, job_id: str, poll_seconds: float = 2.0
) -> list[dict]:
    path = jobs_dir / f"{job_id}.result.json"
    while not path.exists():
        await asyncio.sleep(poll_seconds)
    results = json.loads(path.read_text())["results"]
    path.unlink(missing_ok=True)
    return results
