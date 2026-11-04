"""Resumable OpenRouter transcription and plain LogSeq meeting notes."""

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

import click
import requests

BASE_URL = "https://openrouter.ai/api/v1"
TRANSCRIPTION_MODEL = "openai/whisper-large-v3-turbo"
SUMMARY_MODEL = "anthropic/claude-sonnet-5.5"
CHUNK_SECONDS = 2 * 60 * 60
OVERLAP_SECONDS = 2
MAX_MULTIPART_BYTES = 25_000_000
FORMATS = {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}


PROFILES = {
    "short": "Brief notes: at most 100 words, 2 topics and 4 child bullets. Keep only essential outcomes and concrete next steps.",
    "middle": "Concise notes: at most 180 words, 3 topics and 6 child bullets total. Each child bullet should be 10–25 words. Keep main takeaways and concrete next steps; omit secondary discussion.",
    "long": "Detailed notes: include substantive reasoning, constraints, alternatives and disagreements without repeating the conversation.",
}


class SummaryCommand(click.Command):
    """Allow a bare --summarize before or after AUDIO without consuming its path."""

    def parse_args(self, ctx, args):
        normalized = list(args)
        for index, value in enumerate(normalized):
            if value == "--":
                break
            if value == "--summarize":
                following = normalized[index + 1] if index + 1 < len(normalized) else ""
                if following.lower() not in PROFILES:
                    normalized[index] = "--summarize=middle"
        return super().parse_args(ctx, normalized)


OUTPUT_ROOT = Path("/tmp/stt") if os.name == "posix" else Path(tempfile.gettempdir()) / "stt"


def default_output(audio: Path, suffix: str) -> Path:
    """Keep outputs out of the recording's folder (e.g. Voice Memos temp dirs)."""
    return OUTPUT_ROOT / f"{audio.stem}{suffix}"


def save(path: Path, content: str) -> None:
    """Replace atomically so interrupted writes never destroy a checkpoint."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def request(endpoint: str, api_key: str, **kwargs) -> dict:
    response = requests.post(
        f"{BASE_URL}/{endpoint}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=(15, 180), **kwargs,
    )
    if not response.ok:
        # Avoid dumping provider responses, which can echo audio or credentials.
        raise click.ClickException(
            f"OpenRouter returned HTTP {response.status_code} for {endpoint}. "
            "Check your key, credits, and model availability; rerun to resume."
        )
    data = response.json()
    if not isinstance(data, dict) or "error" in data:
        raise click.ClickException(f"OpenRouter returned an invalid response for {endpoint}.")
    return data


def run_media(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise click.ClickException(f"{Path(args[0]).name} could not decode this recording.")
    return result.stdout


def audio_parts(source: Path, directory: Path):
    """Yield small compressed chunks, or upload supported small files directly."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        duration = float(run_media([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ]).strip())
        if not math.isfinite(duration) or duration <= 0:
            raise click.ClickException("Audio duration must be positive and finite.")
        total = math.ceil(duration / CHUNK_SECONDS)
        for index in range(total):
            yield index, total, index * CHUNK_SECONDS, directory / "part.mp3"
    else:
        if source.suffix.lower().lstrip(".") not in FORMATS:
            raise click.ClickException("Install ffmpeg and ffprobe to decode this audio format.")
        if source.stat().st_size > MAX_MULTIPART_BYTES:
            raise click.ClickException("Install ffmpeg and ffprobe to split audio larger than 25 MB.")
        click.echo("ffmpeg unavailable: uploading directly; long audio may time out.", err=True)
        yield 0, 1, 0, source


def transcribe(source: Path, output: Path, api_key: str, language: str | None,
               force: bool) -> str:
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    signature = {"sha256": digest, "model": TRANSCRIPTION_MODEL,
                 "language": language, "chunk_seconds": CHUNK_SECONDS,
                 "overlap_seconds": OVERLAP_SECONDS,
                 "split": bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))}
    state_path = output / "state.json"
    transcript_path = output / "transcript.txt"
    state = {"signature": signature, "chunks": [], "complete": False}
    if state_path.exists() and not force:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        identity = ("sha256", "model", "language")
        same_recording = all(state.get("signature", {}).get(key) == signature[key] for key in identity)
        if state.get("complete") and transcript_path.exists() and same_recording:
            click.echo("Reusing transcript (including any edits).", err=True)
            return transcript_path.read_text(encoding="utf-8")
        if state.get("signature") != signature:
            raise click.ClickException("Recording or transcription settings changed. Use another -o directory or --force.")
    elif not force and any(output.iterdir()):
        raise click.ClickException("Output directory contains files without a checkpoint. Use another -o directory or --force.")
    if force:
        # A previous summary no longer describes the fresh transcript.
        (output / "notes.md").unlink(missing_ok=True)
        transcript_path.unlink(missing_ok=True)
    save(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    with tempfile.TemporaryDirectory(prefix="stt-") as temporary:
        for index, total, start, part in audio_parts(source, Path(temporary)):
            if index < len(state["chunks"]):
                continue
            click.echo(f"Transcribing part {index + 1}/{total}...", err=True)
            if part != source:
                run_media(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", str(start),
                           "-i", str(source), "-t", str(CHUNK_SECONDS + OVERLAP_SECONDS), "-vn",
                           "-ac", "1", "-ar", "16000", "-b:a", "64k", str(part)])
            data = {"model": TRANSCRIPTION_MODEL, "response_format": "json"}
            if language:
                data["language"] = language
            if part.stat().st_size > MAX_MULTIPART_BYTES:
                # Two-hour chunks can exceed OpenRouter's multipart upload cap.
                data["input_audio"] = {
                    "data": base64.b64encode(part.read_bytes()).decode("ascii"),
                    "format": part.suffix.lower().lstrip("."),
                }
                response = request("audio/transcriptions", api_key, json=data)
            else:
                with part.open("rb") as handle:
                    response = request("audio/transcriptions", api_key,
                                       data=data, files={"file": (part.name, handle)})
            text = response.get("text")
            if not isinstance(text, str):
                raise click.ClickException("Transcription response is missing text.")
            state["chunks"].append({"start": start, "text": text.strip()})
            save(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    chunks = state["chunks"]
    if not any(chunk["text"] for chunk in chunks):
        raise click.ClickException("No speech detected; completed parts are saved in state.json.")
    text = "\n\n".join(
        (f"[{chunk['start'] // 3600:02d}:{chunk['start'] // 60 % 60:02d}:{chunk['start'] % 60:02d}]\n"
         if len(chunks) > 1 else "") + chunk["text"] for chunk in chunks
    ) + "\n"
    save(transcript_path, text)
    state["complete"] = True
    save(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    return text


def participant_labels(speakers: dict[str, str]) -> dict[str, str]:
    """Prefer first names, preserving identity when first names are shared."""
    known = {key: name.strip() for key, name in speakers.items()
             if name.strip() and name.strip() != "name not set"
             and not re.fullmatch(r"P\d+ \(name not set\)", name.strip())}
    first_names = [name.split()[0].casefold() for name in known.values()]
    labels = {}
    for key in speakers:
        if key not in known:
            labels[key] = key
        else:
            first = known[key].split()[0]
            labels[key] = first if first_names.count(first.casefold()) == 1 else known[key]
    return labels


def recent_journals(meeting_date: str | None) -> list[dict[str, str]]:
    """Load the inclusive 30-day window; an empty STT_JOURNAL_DIR opts out."""
    configured = os.environ.get("STT_JOURNAL_DIR")
    if configured == "":
        return []
    directory = Path(configured).expanduser() if configured is not None else (
        Path.home() / "Documents/PersonalKnowledgeBase/journals")
    if not directory.is_dir():
        if configured is not None:
            raise click.ClickException(f"Journal directory does not exist: {directory}")
        return []
    end = date.fromisoformat(meeting_date) if meeting_date else date.today()
    start = end - timedelta(days=29)
    journals = []
    try:
        for path in sorted(directory.glob("*.md")):
            try:
                day = date.fromisoformat(path.stem.replace("_", "-"))
            except ValueError:
                continue
            if start <= day <= end:
                journals.append({"date": day.isoformat(), "file": path.name,
                                 "text": path.read_text(encoding="utf-8")})
    except (OSError, UnicodeError) as exc:
        raise click.ClickException(f"Could not read journal context: {exc}") from exc
    if journals:
        click.echo(f"Including {len(journals)} journal files ({start} through {end}) in notes context.", err=True)
    return journals


def summarize_transcript(text: str, api_key: str, profile: str,
                         speakers: dict[str, str], context: str, meeting_date: str | None) -> str:
    rules = Path(__file__).with_name("stt_notes.md").read_text(encoding="utf-8")
    labels = participant_labels(speakers)
    journals = recent_journals(meeting_date)
    response = request("chat/completions", api_key, json={
        "model": SUMMARY_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": rules + "\nLength profile: " + PROFILES[profile]},
            {"role": "user", "content": json.dumps({
                "participants": speakers, "participant_labels": labels, "meeting_date": meeting_date,
                "background": context, "recent_journals": journals, "transcript": text,
            }, ensure_ascii=False)},
        ],
    })
    choice = response["choices"][0]
    reason = choice.get("finish_reason")
    if reason != "stop":
        label = reason if reason in {"length", "content_filter", "tool_calls", "error"} else "unknown"
        raise click.ClickException(
            f"OpenRouter stopped the notes response (finish_reason={label}). "
            "Transcript and any previous notes are retained."
        )
    content = choice["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise click.ClickException("OpenRouter returned empty notes; transcript and any previous notes are retained.")
    content = content.strip()
    if content.startswith("```") and content.endswith("```"):
        content = "\n".join(content.splitlines()[1:-1]).strip()
    content = content.replace("[[", "").replace("]]", "")
    header = ["- People", *(f"  - {name}" if labels[identifier] != identifier
                            else f"  - {identifier}: {name}" for identifier, name in speakers.items())]
    if meeting_date:
        header.append(f"- Meeting date: {meeting_date}")
    return "\n".join(header) + "\n" + content + "\n"


@click.command(cls=SummaryCommand)
@click.argument("audio", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--summarize", type=click.Choice(list(PROFILES), case_sensitive=False),
              help="Write topic-based LogSeq notes: short, middle, or long. Bare flag uses middle.")
@click.option("--output-dir", "-o", type=click.Path(file_okay=False, path_type=Path),
              help=f"Output folder; default: {OUTPUT_ROOT}/<audio-stem>.stt (or .review).")
@click.option("--speaker", multiple=True, help="Participant name; repeat to assign P1, P2, etc. Does not identify voices.")
@click.option("--context", default="", help="Additional meeting background; recent journals are included automatically (STT_JOURNAL_DIR overrides; empty disables).")
@click.option("--meeting-date", type=click.DateTime(formats=["%Y-%m-%d"]), help="Actual meeting date (YYYY-MM-DD), not the file date.")
@click.option("--language", help="Optional ISO-639-1 transcription language, e.g. en. Default: auto-detect.")
@click.option("--force", is_flag=True, help="Discard previous transcription checkpoint and notes and transcribe again.")
@click.option("--review", is_flag=True, help="Open local speaker review with playback, corrections, and notes (Deepgram Nova-3).")
@click.option("--transcript-json", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="With --review, import an existing diarized response without a transcription charge.")
@click.option("--port", default=0, type=click.IntRange(0, 65535), help="Local review port; 0 selects a free port.")
@click.option("--no-open", is_flag=True, help="Print the review URL without opening a browser.")
def main(audio: Path, summarize: str | None, output_dir: Path | None, speaker: tuple[str, ...],
         context: str, meeting_date, language: str | None, force: bool, review: bool,
         transcript_json: Path | None, port: int, no_open: bool):
    """Transcribe AUDIO using OPENROUTER_API_KEY from the environment.

    \b
    Two separate modes:
      Quick (default)  stt AUDIO [--summarize]
                       Plain transcript in /tmp/stt/<stem>.stt/ (no speaker
                       labels); --summarize also writes notes.md. Reruns reuse
                       the saved transcript, including manual edits.
      Review           stt AUDIO --review
                       Speaker-labelled transcript in a local browser page
                       (Deepgram Nova-3): name speakers, fix passages, then
                       click "Save & generate notes". Saved in
                       /tmp/stt/<stem>.review/; reopens without retranscription.
                       --summarize and --force do not apply here.

    Install ffmpeg and ffprobe for automatic two-hour audio chunks and format
    conversion. Without them, supported files under 25 MB upload directly.
    Chunk labels mark approximate starts, not word or speaker timestamps.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key and not review:
        raise click.ClickException("Set OPENROUTER_API_KEY in your environment.")
    if audio.stat().st_size == 0:
        raise click.ClickException("Audio file is empty.")
    if language and not re.fullmatch(r"[a-z]{2}", language):
        raise click.ClickException("--language must be a two-letter ISO-639-1 code.")
    if any(not name.strip() for name in speaker):
        raise click.ClickException("--speaker names must not be empty.")
    if not review and (transcript_json or port or no_open):
        raise click.ClickException("--transcript-json, --port and --no-open require --review.")
    if review:
        if summarize:
            raise click.ClickException(
                "--summarize does not apply to --review: name speakers in the page, then click "
                "\"Save & generate notes\" there (or run `stt-voice notes` afterwards). Drop --summarize.")
        if force:
            raise click.ClickException(
                "--force does not apply to --review, which keeps your corrections. To transcribe again, "
                "pass a new -o directory or delete the existing review folder.")
        try:
            from my_small_tools.stt_review import launch
        except ModuleNotFoundError as exc:
            raise click.ClickException(
                f"Review needs the '{exc.name}' package, which this install is missing. Reinstall the tool "
                "so new dependencies are picked up, e.g. `uv tool install --reinstall --editable <repo>`.") from exc
        try:
            launch(audio, output_dir or default_output(audio, ".review"), transcript_json,
                   language, speaker, context, meeting_date.strftime("%Y-%m-%d") if meeting_date else None,
                   port, not no_open)
        except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as exc:
            raise click.ClickException(f"Review failed ({type(exc).__name__}); check recording, JSON, output folder, and port.") from exc
        return
    output = (output_dir or default_output(audio, ".stt")).resolve()
    try:
        output.mkdir(parents=True, exist_ok=True)
        transcript = transcribe(audio.resolve(), output, api_key, language, force)
        click.echo(f"Transcript: {output / 'transcript.txt'}")
        if summarize:
            if not transcript.strip():
                raise click.ClickException("Transcript is empty; add text or use --force to transcribe again.")
            speakers = {f"P{i}": name for i, name in enumerate(speaker, 1)} or {
                "P1": "name not set", "P2": "name not set"}
            date = meeting_date.strftime("%Y-%m-%d") if meeting_date else None
            click.echo(f"Writing meeting notes ({summarize})...", err=True)
            notes = summarize_transcript(transcript, api_key, summarize, speakers, context, date)
            save(output / "notes.md", notes)
            click.echo(f"Notes: {output / 'notes.md'}")
    except (OSError, ValueError, KeyError, IndexError, TypeError, requests.RequestException) as exc:
        raise click.ClickException(
            f"STT failed ({type(exc).__name__}). Completed transcription is retained in {output}; "
            "check input/output access or the API response and rerun to resume."
        ) from exc


if __name__ == "__main__":
    main()
