#!/usr/bin/env python3
#
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""
Piper TTS Text-to-Audio Converter with Intelligent Chunking

Features:
- Streams and segments large text files:
  - Prefer paragraphs (blank-line separated)
  - Then sentences (split on .?! followed by whitespace)
  - Fallback to word-based chunks for very long unpunctuated text
- Invokes the Piper executable per chunk and merges into a single WAV
- Auto-detects model/config when given a directory or explicit paths
- Optional speaker selection and voice control parameters
- Writes a CSV chunk map with timestamps and text
- Resume-friendly: skips existing chunk files; can keep or delete chunk WAVs
- Minimal dependencies (standard library + Piper executable on PATH)

Usage example (macOS):
    uv run text_to_speech.py \
        --input /path/to/book.txt \
        --model ~/piper_voices/en_US-amy \
        --output /path/to/book.wav

Install Piper (macOS):
    brew install piper

Get voice model (.onnx and matching .json), place in a directory, and pass that directory to --model.
"""

import argparse
import contextlib
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import wave
from pathlib import Path
from typing import Generator, Iterable, List, Optional, Tuple

# ----------------------------
# Text segmentation utilities
# ----------------------------

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

def iter_paragraphs(path: Path, encoding: str = "utf-8") -> Generator[str, None, None]:
    """
    Stream the file and yield paragraphs separated by at least one blank line.
    This avoids loading the entire file into memory.
    """
    buf: List[str] = []
    with open(path, "r", encoding=encoding, errors="replace", newline=None) as f:
        for line in f:
            if line.strip() == "":
                if buf:
                    yield " ".join(s.strip() for s in buf if s.strip())
                    buf.clear()
            else:
                buf.append(line.rstrip("\n"))
        # tail
        if buf:
            yield " ".join(s.strip() for s in buf if s.strip())


def split_into_sentences(text: str) -> List[str]:
    """
    Basic sentence splitting on punctuation followed by whitespace.
    Not perfect for abbreviations, but robust and dependency-free.
    """
    text = text.strip()
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    # Re-attach punctuation if split removed the boundary char for some reason (rare)
    return [p.strip() for p in parts if p.strip()]


def chunk_sentences(sentences: List[str], max_chars: int) -> List[str]:
    """
    Accumulate sentences into chunks up to max_chars.
    """
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for s in sentences:
        s_len = len(s)
        # +1 for a space if needed
        added = s_len + (1 if cur else 0)
        if cur_len + added <= max_chars:
            cur.append(s)
            cur_len += added
        else:
            if cur:
                chunks.append(" ".join(cur))
                cur = [s]
                cur_len = s_len
            else:
                # Single sentence longer than max_chars -> fallback below
                chunks.extend(chunk_words(s, max_chars))
                cur = []
                cur_len = 0
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def chunk_words(text: str, max_chars: int) -> List[str]:
    """
    Fallback for very long sentences/unpunctuated blocks.
    Uses word-based wrapping without breaking words.
    """
    words = text.split()
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for w in words:
        w_len = len(w)
        added = w_len + (1 if cur else 0)
        if cur_len + added <= max_chars:
            cur.append(w)
            cur_len += added
        else:
            if cur:
                chunks.append(" ".join(cur))
            cur = [w]
            cur_len = w_len
    if cur:
        chunks.append(" ".join(cur))
    # Handle pathological case: a single "word" longer than max_chars
    final_chunks: List[str] = []
    for ch in chunks:
        if len(ch) <= max_chars:
            final_chunks.append(ch)
        else:
            # break long tokens using textwrap with break_long_words=True
            wrapped = textwrap.wrap(ch, width=max_chars, break_long_words=True, break_on_hyphens=False)
            final_chunks.extend(wrapped if wrapped else [ch])
    return final_chunks


def segment_text_streaming(
    path: Path,
    max_chars: int,
    encoding: str = "utf-8",
) -> Generator[str, None, None]:
    """
    Yield chunks of text prioritizing paragraphs, then sentences, with word fallback.
    """
    for para in iter_paragraphs(path, encoding=encoding):
        if not para.strip():
            continue
        if len(para) <= max_chars:
            yield para
            continue
        sentences = split_into_sentences(para)
        if not sentences:
            # no punctuation at all
            for ch in chunk_words(para, max_chars):
                yield ch
        else:
            for ch in chunk_sentences(sentences, max_chars):
                yield ch


# ----------------------------
# Piper invocation
# ----------------------------

def get_audio_params_from_config(config_path: Path) -> Tuple[int, int, int]:
    """Reads sample_rate, channels, and sample_width from Piper model JSON config."""
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    audio_config = data.get("audio", {})

    sample_rate = audio_config.get("sample_rate", data.get("sample_rate"))
    if sample_rate is None:
        raise ValueError(f"Could not find 'sample_rate' in {config_path}")

    # Piper defaults: 1 channel, 16-bit (2 bytes)
    channels = audio_config.get("channels", data.get("channels", 1))
    sample_width = audio_config.get("sample_width", data.get("sample_width", 2))

    return int(sample_rate), int(channels), int(sample_width)


def resolve_model_and_config(model_path: Optional[Path], config_path: Optional[Path]) -> Tuple[Path, Optional[Path]]:
    """
    Given a model path (file or directory) and optional config path, return (onnx_model, config_json|None).
    - If directory provided, pick the first .onnx in it and a matching .json (same stem) if present.
    - If file provided, must be .onnx; try to find a .json with the same stem in the same directory.
    - If model_path is None, try env vars PIPER_MODEL or PIPER_VOICE.
    """
    if model_path is None:
        env_model = os.environ.get("PIPER_MODEL") or os.environ.get("PIPER_VOICE")
        if not env_model:
            raise FileNotFoundError("No model provided. Use --model or set PIPER_MODEL/PIPER_VOICE to a .onnx file or directory.")
        model_path = Path(env_model).expanduser()

    if not model_path.exists():
        raise FileNotFoundError(f"Model path not found: {model_path}")

    onnx_path: Optional[Path] = None
    cfg_path: Optional[Path] = None

    if model_path.is_dir():
        candidates = sorted(model_path.glob("*.onnx"))
        if not candidates:
            raise FileNotFoundError(f"No .onnx model found in directory: {model_path}")
        onnx_path = candidates[0]
        # Find matching .json by stem
        json_candidates = sorted(model_path.glob("*.json"))
        if json_candidates:
            # prefer same stem if exists
            stem_match = [j for j in json_candidates if j.stem == onnx_path.stem]
            cfg_path = stem_match[0] if stem_match else json_candidates[0]
    else:
        # Must be a .onnx file
        if model_path.suffix.lower() != ".onnx":
            raise ValueError(f"--model must be a directory or a .onnx file. Got: {model_path}")
        onnx_path = model_path
        if config_path is None:
            # look for same-stem .json in the same directory
            candidate = onnx_path.with_suffix(".onnx.json")
            if candidate.exists():
                cfg_path = candidate
            else:
                candidate2 = onnx_path.with_suffix(".json")
                if candidate2.exists():
                    cfg_path = candidate2
        else:
            cfg_path = config_path

    return onnx_path, cfg_path


def ensure_piper_available(piper_bin: str) -> None:
    if shutil.which(piper_bin) is None:
        raise RuntimeError(
            f"Cannot find Piper executable '{piper_bin}' on PATH. "
            f"Install Piper (e.g., brew install piper) or provide --piper-bin path."
        )


def synthesize_with_piper(
    text: str,
    out_wav: Path,
    model_path: Path,
    config_path: Optional[Path],
    piper_bin: str,
    sample_rate: int,
    channels: int,
    sample_width: int,
    speaker: Optional[int] = None,
    length_scale: float = 1.0,
    noise_scale: float = 0.667,
    noise_w: float = 0.8,
    overwrite: bool = False,
) -> None:
    """
    Call Piper as a subprocess to get raw audio, then write a proper WAV file.
    This avoids issues with Piper writing incorrect WAV headers for some models.
    """
    if out_wav.exists() and not overwrite:
        return

    cmd = [
        piper_bin,
        "--model", str(model_path),
        "--output_file", "-",  # Raw audio to stdout
        "--length_scale", str(length_scale),
        "--noise_scale", str(noise_scale),
        "--noise_w", str(noise_w),
    ]
    if config_path:
        cmd.extend(["--config", str(config_path)])
    if speaker is not None:
        cmd.extend(["--speaker", str(speaker)])

    # Piper reads text from stdin
    proc = subprocess.run(
        cmd,
        input=(text.strip() + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Piper synthesis failed (exit {proc.returncode}). Command: {cmd}\n{err}")

    # Write WAV file from raw audio bytes
    with contextlib.closing(wave.open(str(out_wav), "wb")) as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(proc.stdout)


# ----------------------------
# WAV merging and mapping
# ----------------------------

def get_wav_params(path: Path) -> Tuple[int, int, int, int]:
    """
    Return (nchannels, sampwidth, framerate, nframes) for a WAV.
    """
    with contextlib.closing(wave.open(str(path), "rb")) as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())


def merge_wavs(
    input_wavs: List[Path],
    output_wav: Path,
    map_csv: Path,
    chunk_texts: List[str],
) -> None:
    """
    Concatenate WAV files ensuring matching audio parameters.
    Also write a CSV map with start/duration seconds, char counts, and text.
    """
    if not input_wavs:
        raise ValueError("No input WAVs to merge.")

    nch, sw, fr, _ = get_wav_params(input_wavs[0])

    # Prepare CSV map
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    with output_wav.open("wb") as fout, \
         contextlib.closing(wave.open(fout, "wb")) as wout, \
         map_csv.open("w", encoding="utf-8", newline="") as csvfile:
        wout.setnchannels(nch)
        wout.setsampwidth(sw)
        wout.setframerate(fr)

        writer = csv.writer(csvfile)
        writer.writerow(["index", "start_sec", "duration_sec", "chars", "text"])

        total_frames = 0
        for idx, wav_path in enumerate(input_wavs):
            with contextlib.closing(wave.open(str(wav_path), "rb")) as win:
                nch2, sw2, fr2, nframes = win.getnchannels(), win.getsampwidth(), win.getframerate(), win.getnframes()
                if (nch2, sw2, fr2) != (nch, sw, fr):
                    raise ValueError(f"Audio params mismatch in {wav_path.name}: "
                                     f"expected {(nch, sw, fr)} got {(nch2, sw2, fr2)}")

                start_sec = total_frames / fr
                # Stream frames to avoid large memory footprint
                chunk = 4096
                frames_copied = 0
                while True:
                    data = win.readframes(chunk)
                    if not data:
                        break
                    frames = len(data) // (sw * nch)
                    frames_copied += frames
                    wout.writeframes(data)

                duration_sec = frames_copied / fr
                total_frames += frames_copied
                text = chunk_texts[idx] if idx < len(chunk_texts) else ""
                writer.writerow([idx, f"{start_sec:.3f}", f"{duration_sec:.3f}", len(text), text])


# ----------------------------
# CLI and Orchestration
# ----------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert a text file to speech using Piper with intelligent chunking."
    )
    p.add_argument("-i", "--input", required=True, help="Path to input text file.")
    p.add_argument("-m", "--model", default=None, help="Path to a Piper model (.onnx) or directory containing it.")
    p.add_argument("-c", "--config", default=None, help="Path to model .json config (optional; auto-detected if omitted).")
    p.add_argument("-o", "--output", default=None, help="Path to merged output WAV (default: <input_stem>.wav in current dir).")
    p.add_argument("--outdir", default=None, help="Directory for chunks and map (default: <output>_chunks alongside output).")
    p.add_argument("--keep-chunks", action="store_true", help="Keep per-chunk WAVs after merging.")
    p.add_argument("--no-merge", action="store_true", help="Do not merge chunks; only produce per-chunk WAVs and CSV.")
    p.add_argument("--max-chars", type=int, default=1200, help="Max characters per TTS chunk (default: 1200).")
    p.add_argument("--encoding", default="utf-8", help="Input file encoding (default: utf-8).")
    p.add_argument("--piper-bin", default="piper", help="Piper executable name or path (default: 'piper').")
    p.add_argument("--speaker", type=int, default=None, help="Speaker index for multi-speaker models (optional).")
    p.add_argument("--length-scale", type=float, default=1.0, help="Piper length_scale (default: 1.0).")
    p.add_argument("--noise-scale", type=float, default=0.667, help="Piper noise_scale (default: 0.667).")
    p.add_argument("--noise-w", type=float, default=0.8, help="Piper noise_w (default: 0.8).")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing chunk files.")
    p.add_argument("--dry-run", action="store_true", help="Only show chunking plan; do not synthesize.")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        print(f"Input not found: {input_path}", file=sys.stderr)
        sys.exit(2)

    config_path = Path(args.config).expanduser() if args.config else None
    model_path = Path(args.model).expanduser() if args.model else None

    try:
        onnx_model, cfg = resolve_model_and_config(model_path, config_path)
        if cfg is None:
            raise FileNotFoundError(f"Could not find .json config file for model: {onnx_model.parent}")
        audio_params = get_audio_params_from_config(cfg)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(2)

    try:
        ensure_piper_available(args.piper_bin)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(2)

    # Determine output path and dirs
    if args.output:
        output_wav = Path(args.output).expanduser()
    else:
        output_wav = Path.cwd() / f"{input_path.stem}.wav"
    output_wav.parent.mkdir(parents=True, exist_ok=True)

    outdir = Path(args.outdir).expanduser() if args.outdir else output_wav.with_suffix("").with_name(output_wav.stem + "_chunks")
    chunks_dir = outdir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    map_csv = outdir / "chunks_map.csv"

    print(f"[info] Input:          {input_path}")
    print(f"[info] Model:          {onnx_model}")
    print(f"[info] Config:         {cfg if cfg else '(none)'}")
    print(f"[info] Audio params:   {audio_params[0]} Hz, {audio_params[1]}ch, {audio_params[2]*8}-bit")
    print(f"[info] Output WAV:     {output_wav}")
    print(f"[info] Chunks dir:     {chunks_dir}")
    print(f"[info] Chunk map CSV:  {map_csv}")
    print(f"[info] Max chars:      {args.max_chars}")
    print(f"[info] Piper bin:      {args.piper_bin}")
    print(f"[info] Speaker:        {args.speaker if args.speaker is not None else '(default)'}")
    print(f"[info] length_scale:   {args.length_scale}")
    print(f"[info] noise_scale:    {args.noise_scale}")
    print(f"[info] noise_w:        {args.noise_w}")

    # Segment the text
    t0 = time.time()
    planned_chunks: List[str] = list(segment_text_streaming(input_path, args.max_chars, encoding=args.encoding))
    if not planned_chunks:
        print("[warn] No text found to synthesize.", file=sys.stderr)
        sys.exit(0)

    print(f"[info] Chunks planned: {len(planned_chunks)}")
    if args.dry_run:
        # Preview up to first 5 chunks
        preview = min(5, len(planned_chunks))
        print(f"[dry-run] Showing first {preview} chunks:")
        for i in range(preview):
            snippet = planned_chunks[i]
            print(f"  - chunk {i:04d} (len={len(snippet)}): {snippet[:120]!r}{'...' if len(snippet) > 120 else ''}")
        print("[dry-run] Synthesis not performed.")
        return

    # Synthesize each chunk with Piper
    chunk_paths: List[Path] = []
    for idx, txt in enumerate(planned_chunks):
        chunk_wav = chunks_dir / f"chunk_{idx:04d}.wav"
        chunk_paths.append(chunk_wav)

        if chunk_wav.exists() and not args.overwrite:
            print(f"[skip] {idx+1}/{len(planned_chunks)} exists: {chunk_wav.name}")
            continue

        print(f"[tts ] {idx+1}/{len(planned_chunks)} -> {chunk_wav.name} (chars={len(txt)})")
        try:
            synthesize_with_piper(
                text=txt,
                out_wav=chunk_wav,
                model_path=onnx_model,
                config_path=cfg,
                piper_bin=args.piper_bin,
                sample_rate=audio_params[0],
                channels=audio_params[1],
                sample_width=audio_params[2],
                speaker=args.speaker,
                length_scale=args.length_scale,
                noise_scale=args.noise_scale,
                noise_w=args.noise_w,
                overwrite=args.overwrite,
            )
        except Exception as e:
            print(f"[error] Chunk {idx} failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Merge unless requested not to
    if not args.no_merge:
        print(f"[merge] Merging {len(chunk_paths)} chunks -> {output_wav.name}")
        try:
            merge_wavs(chunk_paths, output_wav, map_csv, planned_chunks)
        except Exception as e:
            print(f"[error] Merge failed: {e}", file=sys.stderr)
            sys.exit(1)

        if not args.keep_chunks:
            # Try to delete chunks; leave map.csv and directory
            deleted = 0
            for p in chunk_paths:
                try:
                    p.unlink()
                    deleted += 1
                except Exception:
                    pass
            print(f"[clean] Deleted {deleted}/{len(chunk_paths)} chunk files.")
    else:
        # Still write the map CSV if not merged
        print(f"[map  ] Writing chunk map CSV without merging.")
        # We need sample-accurate timestamps to write a real map; without merging we don't know durations yet.
        # As a fallback, after synthesis, we can still compute durations of the chunks and simulate merge offsets.
        # Do that here:
        try:
            # Compute durations and write a map with simulated start/duration
            start_sec = 0.0
            with map_csv.open("w", encoding="utf-8", newline="") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["index", "start_sec", "duration_sec", "chars", "text"])
                for idx, p in enumerate(chunk_paths):
                    nch, sw, fr, nframes = get_wav_params(p)
                    duration_sec = nframes / fr
                    writer.writerow([idx, f"{start_sec:.3f}", f"{duration_sec:.3f}", len(planned_chunks[idx]), planned_chunks[idx]])
                    start_sec += duration_sec
        except Exception as e:
            print(f"[warn] Failed to compute chunk map without merging: {e}", file=sys.stderr)

    dt = time.time() - t0
    print(f"[done] Completed in {dt:.1f}s")


if __name__ == "__main__":
    main()
