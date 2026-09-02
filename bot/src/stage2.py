"""Stage 2: assemble the LLM request from the transcribed batch."""

from __future__ import annotations

from pathlib import Path

from .batch import Batch, MediaItem


def build_transcript_text(media: list[MediaItem], output_dir: Path) -> str:
    """Concatenate transcripts, each under a `=== [N] name ===` heading.

    The headings are what make per-item corrections work ("fix the second one").
    """
    blocks = []
    for item in media:
        if not item.transcript_path:
            continue
        text = (output_dir / item.transcript_path).read_text().strip()
        blocks.append(f"=== [{item.index}] {Path(item.filename).stem} ===\n{text}")
    return "\n\n".join(blocks)


def build_system_prompt(metaprompt_path: Path, batch: Batch) -> str:
    # Metaprompt is read fresh every pass so host edits take effect on the next
    # correction without a restart.
    parts = [metaprompt_path.read_text().strip()]
    parts += [p.strip() for p in batch.extra_prompt]
    parts += [c.strip() for c in batch.corrections]
    return "\n\n".join(p for p in parts if p)
