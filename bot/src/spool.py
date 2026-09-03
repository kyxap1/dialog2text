"""Filesystem spool between the bot container and the host worker.

Single writer on each side: the bot writes *.job.json and *.llm.json, the
worker writes *.result.json. See the design doc's "Spool protocol" section.
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


def _write(jobs_dir: Path, name: str, payload: dict) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / name
    tmp = jobs_dir / f".{name}.tmp"
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)  # atomic: the worker never sees a half-written job
    return path


def write_job(jobs_dir: Path, job_id: str, media: list[str]) -> Path:
    return _write(jobs_dir, f"{job_id}.job.json", {"media": media})


def write_llm_job(
    jobs_dir: Path, job_id: str, model: str, system: str, text: str, thinking: bool
) -> Path:
    return _write(
        jobs_dir,
        f"{job_id}.llm.json",
        {"model": model, "system": system, "text": text, "thinking": thinking},
    )


async def _await_result(jobs_dir: Path, job_id: str, poll_seconds: float) -> dict:
    path = jobs_dir / f"{job_id}.result.json"
    while not path.exists():
        await asyncio.sleep(poll_seconds)
    payload = json.loads(path.read_text())
    path.unlink(missing_ok=True)
    return payload


async def await_result(
    jobs_dir: Path, job_id: str, poll_seconds: float = 2.0
) -> list[dict]:
    return (await _await_result(jobs_dir, job_id, poll_seconds))["results"]


async def await_llm_result(
    jobs_dir: Path, job_id: str, poll_seconds: float = 2.0
) -> dict:
    return await _await_result(jobs_dir, job_id, poll_seconds)
