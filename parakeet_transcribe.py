#!/usr/bin/env python3
"""Transcribe + diarize audio with NVIDIA Parakeet (MLX), reusing whispermlx's
diarization pipeline and output writers -- those stages only need
segments as {start, end, text}, they don't care which ASR produced them.
"""
import argparse
import os

import mlx.core as mx
from parakeet_mlx import from_pretrained
from whispermlx.diarize import DiarizationPipeline
from whispermlx.diarize import assign_word_speakers
from whispermlx.utils import get_writer

if mem_limit := os.environ.get("MEM_LIMIT_BYTES"):
    mx.set_memory_limit(int(mem_limit))

MODEL_MAP = {
    "parakeet-v2": "mlx-community/parakeet-tdt-0.6b-v2",
    "parakeet-v3": "mlx-community/parakeet-tdt-0.6b-v3",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", nargs="+", help="audio/video file(s) to transcribe")
    parser.add_argument("--hf_token")
    parser.add_argument("--model", default="parakeet-v3", help="short name or full HF repo id")
    parser.add_argument("--model_dir", help="HF cache dir for the ASR and diarization models")
    parser.add_argument("--output_dir", "-o", default=".")
    parser.add_argument("--output_format", default="txt", choices=["all", "srt", "vtt", "txt", "tsv", "json"])
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--min_speakers", type=int)
    parser.add_argument("--max_speakers", type=int)
    parser.add_argument("--diarize_model", default="pyannote/speaker-diarization-community-1")
    # Without chunking, parakeet-mlx runs one encoder pass over the whole file;
    # self-attention is O(n^2), so a ~37 min clip asks Metal for ~50 GB and aborts.
    # 180 s peaks near 4 GB -- lower it if a memory-constrained machine still OOMs.
    parser.add_argument("--chunk_duration", type=float, default=180.0)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    repo = MODEL_MAP.get(args.model, args.model)
    model = from_pretrained(repo, cache_dir=args.model_dir)

    diarize_model = None
    if args.diarize:
        diarize_model = DiarizationPipeline(
            model_name=args.diarize_model, token=args.hf_token, cache_dir=args.model_dir
        )

    writer = get_writer(args.output_format, args.output_dir)
    # only txt/srt/vtt read these; json/tsv ignore them
    writer_args = {"highlight_words": False, "max_line_count": None, "max_line_width": None}

    for audio_path in args.audio:
        print(f"Transcribing {audio_path}...")
        aligned = model.transcribe(audio_path, chunk_duration=args.chunk_duration)
        result = {
            "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in aligned.sentences],
            "language": "multilingual",
        }
        if diarize_model is not None:
            print("Diarizing...")
            diarize_df = diarize_model(
                audio_path, min_speakers=args.min_speakers, max_speakers=args.max_speakers
            )
            result = assign_word_speakers(diarize_df, result)
        writer(result, audio_path, writer_args)


if __name__ == "__main__":
    main()
