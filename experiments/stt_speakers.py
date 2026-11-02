"""Probe OpenRouter's actual speaker/timestamp output without changing stt.

Run with uv run experiments/stt_speakers.py --help. Results contain private
transcripts: choose an output directory outside the repository. Each selected
case uploads the excerpt separately and incurs the model's normal API charge.
Requires OPENROUTER_API_KEY, ffmpeg, and ffprobe.
"""

import base64
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import click
import requests

BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "nvidia/nemotron-3.5-transcribe"
SPEAKER_PROMPT = (
    "Transcribe this audio with punctuation and capitalization. "
    "Include all spoken words and verbal fillers. "
    "Write numbers in their standard written form. "
    "Put speaker tags <spk:0> <spk:1> <spk:2> <spk:3> on the transcription."
)
DEFAULT_CASES = ("nemotron", "whisper", "nemotron-speakers", "nemotron-chat")
CASES = (*DEFAULT_CASES, "nova3-diarize")


def payload_for(case: str, encoded: str) -> tuple[str, dict]:
    """Keep the exact tested parameters explicit; never infer speaker identities."""
    payload = {
        "model": MODEL,
        "input_audio": {"data": encoded, "format": "mp3"},
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment", "word"],
    }
    if case == "whisper":
        payload["model"] = "openai/whisper-large-v3-turbo"
    elif case == "nova3-diarize":
        payload["model"] = "deepgram/nova-3"
        payload["provider"] = {"options": {"deepgram": {
            "diarize": True, "punctuate": True, "smart_format": True,
        }}}
    elif case == "nemotron-speakers":
        # Endpoint discovery reported deepinfra/bf16. initial_prompt and
        # chunk_level are in DeepInfra's published native input schema.
        # This tests forwarding; HTTP 200 does not establish option support.
        payload["provider"] = {"options": {"deepinfra/bf16": {
            "initial_prompt": SPEAKER_PROMPT, "chunk_level": "word",
        }}}
    elif case == "nemotron-chat":
        return "chat/completions", {
            "model": MODEL, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": SPEAKER_PROMPT},
                {"type": "input_audio", "input_audio": payload["input_audio"]},
            ]}],
        }
    return "audio/transcriptions", payload


def metrics_for(data: dict) -> dict:
    segments = data.get("segments") or []
    words = data.get("words") or []
    speakers = {
        str(row["speaker"]) for row in segments + words
        if row.get("speaker") is not None
    }
    return {
        "response_keys": sorted(data), "usage": data.get("usage"),
        "segments": len(segments), "words": len(words),
        "speaker_labels": sorted(speakers),
        "inline_speaker_tags": sorted(set(re.findall(r"<spk:\d+>", data.get("text", "")))),
        "segment_spans_seconds": [round(row["end"] - row["start"], 3)
                                  for row in segments
                                  if "start" in row and "end" in row],
    }


def speaker_transcript(data: dict) -> str | None:
    """Group adjacent labeled segments without guessing identities or editing text."""
    segments = data.get("segments") or []
    if not any(row.get("speaker") is not None for row in segments):
        return None
    turns = []
    for row in segments:
        speaker = row.get("speaker")
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["text"] += " " + row["text"].strip()
            turns[-1]["end"] = row["end"]
        else:
            turns.append({"speaker": speaker, "start": row["start"],
                          "end": row["end"], "text": row["text"].strip()})
    # Use readable labels in order of appearance, retaining raw IDs in the header.
    labels = {}
    for turn in turns:
        if turn["speaker"] is not None:
            labels.setdefault(turn["speaker"], f"Speaker {len(labels) + 1}")
    def timestamp(seconds):
        minutes, seconds = divmod(int(seconds), 60)
        return f"{minutes:02d}:{seconds:02d}"
    header = "Automatic speaker labels; identities and attribution need review.\n"
    header += "; ".join(f"{name} = provider speaker {key}" for key, name in labels.items())
    body = [f"[{timestamp(turn['start'])}–{timestamp(turn['end'])}] "
            f"{labels.get(turn['speaker'], 'Unknown speaker')}\n"
            + textwrap.fill(turn["text"], width=100, break_long_words=False,
                            break_on_hyphens=False) for turn in turns]
    return header + "\n\n" + "\n\n".join(body) + "\n"


@click.command()
@click.argument("audio", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output-dir", "-o", required=True,
              type=click.Path(file_okay=False, path_type=Path), help="New, private results folder.")
@click.option("--start", default=0.0, type=click.FloatRange(min=0), show_default=True,
              help="Excerpt start in seconds; response timestamps are relative to it.")
@click.option("--seconds", default=90.0, type=click.FloatRange(min=0, min_open=True),
              show_default=True, help="Excerpt length. Model limit: one hour.")
@click.option("--case", "cases", multiple=True, type=click.Choice(CASES),
              help="Repeat to choose probes. Default: all four, each billed separately.")
@click.option("--transport", type=click.Choice(["json", "multipart"]), default="json",
              show_default=True, help="Multipart avoids the large JSON input routing limit.")
def main(audio: Path, output_dir: Path, start: float, seconds: float,
         cases: tuple[str, ...], transport: str):
    """Compare transcription capabilities on the same excerpt of AUDIO.

    Retains raw responses, safe request settings, measured latency, billed cost,
    and detected speaker/timestamp fields. No quality score without ground truth.
    A successful HTTP response does not guarantee diarization or word alignment.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise click.ClickException("Set OPENROUTER_API_KEY in your environment.")
    if not all(math.isfinite(value) for value in (start, seconds)) or seconds > 3600:
        raise click.ClickException("Use finite start/length values and at most 3600 seconds.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise click.ClickException("Install ffmpeg and ffprobe to prepare identical excerpts.")
    if output_dir.exists():
        raise click.ClickException("Choose a new output directory; existing results are never overwritten.")
    try:
        output_dir.mkdir(parents=True)
        sample = output_dir / "sample.mp3"
        media = subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", str(start),
            "-i", str(audio.resolve()), "-t", str(seconds), "-vn", "-ac", "1",
            "-ar", "16000", "-b:a", "64k", str(sample),
        ], capture_output=True, check=False)
        if media.returncode or not sample.exists() or not sample.stat().st_size:
            raise click.ClickException("ffmpeg could not prepare the excerpt.")
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(sample),
        ], capture_output=True, text=True, check=False)
        if probe.returncode:
            raise click.ClickException("ffprobe could not measure the excerpt.")
        duration = float(probe.stdout.strip())
        encoded = base64.b64encode(sample.read_bytes()).decode("ascii")
        results = []
        for case in dict.fromkeys(cases or DEFAULT_CASES):
            endpoint, payload = payload_for(case, encoded)
            # Save settings without the audio/base64 payload or credentials.
            settings = {k: v for k, v in payload.items() if k not in {"input_audio", "messages"}}
            if case == "nemotron-chat":
                settings["prompt"] = SPEAKER_PROMPT
            click.echo(f"Running {case} ({duration:.1f}s excerpt)...", err=True)
            started = time.monotonic()
            result = {"case": case, "endpoint": endpoint, "settings": settings,
                      "start_seconds": start, "audio_seconds": duration,
                      "transport": transport if endpoint == "audio/transcriptions" else "json"}
            try:
                options = {"headers": {"Authorization": f"Bearer {key}"}, "timeout": (15, 180)}
                if transport == "multipart" and endpoint == "audio/transcriptions":
                    form = {k: v for k, v in payload.items()
                            if k not in {"input_audio", "timestamp_granularities", "provider"}}
                    form["timestamp_granularities[]"] = payload["timestamp_granularities"]
                    if "provider" in payload:
                        form["provider"] = json.dumps(payload["provider"])
                    with sample.open("rb") as handle:
                        response = requests.post(f"{BASE_URL}/{endpoint}", data=form,
                                                 files={"file": (sample.name, handle)}, **options)
                else:
                    response = requests.post(f"{BASE_URL}/{endpoint}", json=payload, **options)
                result["status"] = response.status_code
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Expected a response object")
                (output_dir / f"{case}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                result.update(metrics_for(data))
                readable = speaker_transcript(data) if response.ok else None
                if readable:
                    (output_dir / f"{case}-speakers.txt").write_text(readable, encoding="utf-8")
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                # Do not print response bodies or exception messages containing input.
                result["error_type"] = type(exc).__name__
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            results.append(result)
            (output_dir / "metrics.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            click.echo(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Experiment failed ({type(exc).__name__}); inspect {output_dir}.") from exc


if __name__ == "__main__":
    main()
