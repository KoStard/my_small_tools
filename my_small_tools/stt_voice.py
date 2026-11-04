"""Local, opt-in voice enrollment and conservative name suggestions.

WeSpeaker ResNet34-LM (CC BY 4.0):
https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM
Features follow WeSpeaker: 16 kHz PCM, 80-bin Hamming fbank, mean normalization.
Similarity scores are cosine similarities, never calibrated probabilities.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from functools import wraps
from pathlib import Path

import appdirs
import click
import numpy as np
import requests

from my_small_tools.stt import OUTPUT_ROOT, default_output, save

MODEL_REVISION = "f0c48c298fd835726c27956a5d617bad7115627e"
MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
MODEL_URL = ("https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/"
             f"{MODEL_REVISION}/voxceleb_resnet34_LM.onnx")
MODEL_ID = f"wespeaker-resnet34-LM:{MODEL_REVISION}:fbank80-hamming-cmn-v1"
# WeSpeaker's ONNX example uses a 0.5 verification threshold. Enrollment can
# raise it using observed separation from other voices, never lower it.
MIN_SIMILARITY = 0.5


def unit(vector):
    value = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(value)
    if value.shape != (256,) or not np.isfinite(value).all() or norm < 1e-8:
        raise click.ClickException("Could not extract a usable voice signature.")
    return value / norm


def sample_windows(segments, speaker, limit=5):
    """Prefer separate turns; use disjoint five-second samples if turns are few."""
    rows = sorted((row for row in segments if row["speaker"] == speaker
                   and row["end"] - row["start"] >= 4),
                  key=lambda row: row["end"] - row["start"], reverse=True)
    windows = []
    for row in rows:
        length = min(5, row["end"] - row["start"])
        start = (row["start"] + row["end"] - length) / 2
        windows.append((start, length))
        if len(windows) == limit:
            return windows
    for row in rows:
        start = row["start"]
        while start + 5 <= row["end"]:
            if all(start + 5 <= other or start >= other + length for other, length in windows):
                windows.append((start, 5))
                if len(windows) == limit:
                    return windows
            start += 5
    return windows


class VoiceMemory:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path(appdirs.user_data_dir("my_small_tools")) / "voice"
        self.profile_path = self.directory / "me.json"
        self.lock = threading.RLock()
        self.model = None

    def path_for(self, profile_id):
        if profile_id == "me":
            return self.profile_path  # Preserve existing personal enrollment without migration.
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-f0-9]{32}", profile_id):
            raise click.ClickException("Invalid remembered person.")
        return self.directory / "people" / f"{profile_id}.json"

    def profile(self, profile_id="me"):
        with self.lock:
            path = self.path_for(profile_id)
            if not path.exists():
                return None
            profile = json.loads(path.read_text(encoding="utf-8"))
            if profile.get("model") != MODEL_ID:
                raise click.ClickException("The saved voice uses a different model; enroll your voice again.")
            unit(profile["centroid"])
            if (not isinstance(profile.get("name"), str) or not profile["name"].strip()
                    or not isinstance(profile.get("samples"), list) or not 3 <= len(profile["samples"]) <= 5
                    or not isinstance(profile.get("threshold"), (float, int))
                    or not .5 <= profile["threshold"] <= 1
                    or not isinstance(profile.get("separated"), bool)):
                raise click.ClickException("The saved voice profile is invalid; enroll again or forget it.")
            for sample in profile["samples"]:
                unit(sample)
            return profile

    def profiles(self):
        """A damaged profile must not prevent reviewing audio or using other people."""
        ids = (["me"] if self.profile_path.exists() else [])
        ids += sorted(path.stem for path in (self.directory / "people").glob("*.json"))
        profiles, broken = {}, []
        for identifier in ids:
            try:
                profile = self.profile(identifier)
                if profile:
                    profiles[identifier] = profile
            except (click.ClickException, ValueError, KeyError, TypeError, OSError, AttributeError):
                broken.append(identifier)
        return profiles, broken

    def public(self):
        profiles, broken = self.profiles()
        me = profiles.get("me")
        result = {"enrolled": bool(profiles), "name": me["name"] if me else "",
                  "samples": len(me["samples"]) if me else 0,
                  "profiles": [{"id": key, "name": profile["name"], "samples": len(profile["samples"])}
                               for key, profile in profiles.items()], "broken": broken}
        if broken:
            result["error"] = "A saved voice profile cannot be read; forget it or enroll that person again."
        return result

    def load_model(self):
        if self.model is not None:
            return self.model
        import onnxruntime as ort
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / "wespeaker-resnet34-LM.onnx"
        if not path.exists():
            click.echo("Downloading the local voice model (26.5 MB; no audio is uploaded)...", err=True)
            temporary = None
            try:
                with requests.get(MODEL_URL, stream=True, timeout=(15, 120)) as response:
                    response.raise_for_status()
                    digest, size = hashlib.sha256(), 0
                    with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as handle:
                        temporary = Path(handle.name)
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > 30_000_000:
                                raise click.ClickException("Voice model download exceeded its expected size.")
                            digest.update(chunk)
                            handle.write(chunk)
                    if digest.hexdigest() != MODEL_SHA256:
                        raise click.ClickException("Voice model checksum did not match; download was not installed.")
                    temporary.replace(path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != MODEL_SHA256:
                raise click.ClickException("The local voice model is damaged; remove its ONNX file and retry.")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.model = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        return self.model

    def embedding(self, audio, start, length):
        import kaldi_native_fbank as knf
        try:
            result = subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin", "-ss", str(start), "-i", str(audio),
                "-t", str(length), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
            ], capture_output=True, check=False, timeout=30)
        except FileNotFoundError as exc:
            raise click.ClickException("Install ffmpeg to remember or match your voice locally.") from exc
        if result.returncode:
            raise click.ClickException("ffmpeg could not read a voice sample.")
        samples = np.frombuffer(result.stdout, dtype="<i2").astype(np.float32)
        if len(samples) < 16000 * 3 or np.sqrt(np.mean(samples ** 2)) < 32:
            return None  # Too short or nearly silent; do not treat it as a voice match.
        options = knf.FbankOptions()
        options.frame_opts.samp_freq = 16000
        options.frame_opts.dither = 0
        options.frame_opts.window_type = "hamming"
        options.frame_opts.snip_edges = True
        options.mel_opts.num_bins = 80
        fbank = knf.OnlineFbank(options)
        fbank.accept_waveform(16000, samples.tolist())
        fbank.input_finished()
        features = np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)])
        features -= features.mean(axis=0, keepdims=True)
        model = self.load_model()
        output = model.run(None, {model.get_inputs()[0].name: features[np.newaxis].astype(np.float32)})[0]
        return unit(output.reshape(-1))

    def vectors(self, audio, state, speaker):
        found = []
        for start, length in sample_windows(state["segments"], speaker):
            vector = self.embedding(audio, start, length)
            if vector is not None:
                found.append(vector)
        return found

    def enroll(self, audio, state, speaker, name, profile_id="me"):
        if (speaker not in state["speakers"] or not isinstance(name, str) or not name.strip()
                or len(name) > 200 or any(c in name for c in "\r\n")):
            raise click.ClickException("Choose your speaker and enter a single-line name.")
        with self.lock:
            profiles, _ = self.profiles()
            creating = profile_id is None
            if profile_id is None:
                if any(profile["name"].casefold() == name.strip().casefold() for profile in profiles.values()):
                    raise click.ClickException("That name is already remembered; select the existing person to update their voice.")
                profile_id = uuid.uuid4().hex
            path = self.path_for(profile_id)
            if not creating and profile_id != "me" and not path.exists():
                raise click.ClickException("The selected person is no longer remembered.")
            if any(key != profile_id and profile["name"].casefold() == name.strip().casefold()
                   for key, profile in profiles.items()):
                raise click.ClickException("That name belongs to another remembered person; select their profile to update it.")
            vectors = self.vectors(audio, state, speaker)
            if len(vectors) < 3:
                raise click.ClickException("Need three clear, non-overlapping voice samples of at least four seconds; correct the turns first.")
            matrix = np.stack(vectors)
            similarities = matrix @ matrix.T
            medoid = matrix[np.argmax(np.median(similarities, axis=1))]
            matrix = matrix[matrix @ medoid >= MIN_SIMILARITY]
            if len(matrix) < 3:
                raise click.ClickException("These samples do not consistently sound like one voice; correct the speaker labels and try again.")
            centroid = unit(matrix.mean(axis=0))
            own_scores = [float(vector @ unit(np.delete(matrix, i, axis=0).mean(axis=0)))
                          for i, vector in enumerate(matrix)]
            others = [vector for other in state["speakers"] if other != speaker
                      for vector in self.vectors(audio, state, other)]
            negative = max((float(vector @ centroid) for vector in others), default=MIN_SIMILARITY)
            positive = min(own_scores)
            separated = positive > max(MIN_SIMILARITY, negative)
            threshold = min(1.0, max(MIN_SIMILARITY, (positive + negative) / 2))
            # If enrollment cannot separate the voices, retain the profile but
            # require a question for every future candidate instead of claiming a match.
            profile = {"version": 1, "model": MODEL_ID, "name": name.strip(),
                       "centroid": centroid.tolist(), "samples": matrix.tolist(),
                       "threshold": threshold, "separated": separated,
                       "calibration": {"own_min": positive, "other_max": negative},
                       "recording_sha256": state["signature"]["sha256"]}
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            save(path, json.dumps(profile, ensure_ascii=False, indent=2))
            os.chmod(path, 0o600)
            return {**self.public(), "saved_id": profile_id, "saved_name": profile["name"]}

    def suggest(self, audio, state):
        with self.lock:
            profiles, _ = self.profiles()
            if not profiles:
                return {**self.public(), "suggestions": {}}
            candidates = {}
            for speaker in state["speakers"]:
                vectors = self.vectors(audio, state, speaker)
                matches = []
                for profile_id, profile in profiles.items():
                    scores = [float(vector @ unit(profile["centroid"])) for vector in vectors]
                    score = float(np.median(scores)) if scores else None
                    strong = (len(scores) >= 3 and min(scores) >= profile["threshold"] and profile["separated"])
                    if score is None or score >= MIN_SIMILARITY:
                        matches.append({"profile_id": profile_id, "name": profile["name"], "score": score,
                                        "samples": len(scores), "strong": bool(strong)})
                matches.sort(key=lambda item: item["score"] if item["score"] is not None else -1, reverse=True)
                candidates[speaker] = matches
            # Abstain when multiple identities fit, or one identity fits multiple
            # clusters. A narrow numerical lead is not proof of identity.
            strong_counts = {}
            for matches in candidates.values():
                for match in matches:
                    if match["strong"]:
                        key = match["profile_id"]
                        strong_counts[key] = strong_counts.get(key, 0) + 1
            suggestions = {}
            for speaker, matches in candidates.items():
                confident = (len(matches) == 1 and matches[0]["strong"]
                             and strong_counts[matches[0]["profile_id"]] == 1)
                choices = [{k: v for k, v in item.items() if k != "strong"} for item in matches]
                best = choices[0] if choices else {"name": "", "score": None, "samples": 0}
                suggestions[speaker] = {**best, "status": "suggested" if confident else "unsure" if choices else "no_match",
                                        "choices": choices}
            return {**self.public(), "suggestions": suggestions}

    def forget(self, profile_id="me"):
        with self.lock:
            self.path_for(profile_id).unlink(missing_ok=True)
        return self.public()


@click.group()
def cli():
    """Manage local remembered voices and speaker names without opening a browser.

    Uses the same profiles and review checkpoints as stt --review. Matching
    suggests names; only explicit name/remember commands change attribution.
    """


def review_command(function):
    """Share a recording selector; import explicitly, never transcribe implicitly."""
    @click.argument("audio", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option("--output-dir", "-o", type=click.Path(file_okay=False, path_type=Path),
                  help=f"Review folder; default: {OUTPUT_ROOT}/<audio-stem>.review.")
    @click.option("--transcript-json", type=click.Path(exists=True, dir_okay=False, path_type=Path),
                  help="Initialize a review from a full-recording diarized JSON response.")
    @wraps(function)
    def wrapped(audio, output_dir, transcript_json, **kwargs):
        from my_small_tools.stt_review import ReviewSession
        output = output_dir or default_output(audio, ".review")
        checkpoint = output / "review.json"
        if not checkpoint.exists() and transcript_json is None:
            raise click.ClickException("No saved review. Run stt AUDIO --review or supply --transcript-json; this command never buys a transcription.")
        try:
            language = json.loads(checkpoint.read_text())["signature"].get("language") if checkpoint.exists() else None
            session = ReviewSession(audio, output, transcript_json, language=language)
            return function(session=session, **kwargs)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(f"Voice command failed ({type(exc).__name__}); saved review and profiles are retained.") from exc
    return wrapped


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit metadata without voice embeddings.")
def list_people(as_json):
    """List remembered people and stable profile IDs."""
    public = VoiceMemory().public()
    if as_json:
        click.echo(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        for profile in public["profiles"]:
            click.echo(f"{profile['id']}\t{profile['name']}\t{profile['samples']} samples")
        if not public["profiles"]:
            click.echo("No remembered voices.")
        for identifier in public["broken"]:
            click.echo(f"{identifier}\tunreadable profile", err=True)


@cli.command()
@review_command
@click.option("--speaker", required=True, help="Speaker identifier from 'speakers', e.g. P1.")
@click.option("--name", required=True, help="Confirmed name for this speaker.")
@click.option("--profile-id", help="Update this existing person; omit to create a new person.")
@click.option("--me", is_flag=True, help="Update your personal profile (ID me).")
def remember(session, speaker, name, profile_id, me):
    """Remember a named voice locally and save its name in the review.

    Requires ffmpeg and three clear samples; downloads the 26.5 MB model on
    first use. No voice data is sent away for matching or enrollment.
    """
    if me and profile_id:
        raise click.ClickException("Choose --me or --profile-id, not both.")
    result = session.remember_voice(session.snapshot()["revision"], speaker, name,
                                    "me" if me else profile_id)
    click.echo(f"Remembered: {result['name']} ({result['voice']['saved_id']})")


@cli.command()
@review_command
@click.option("--json", "as_json", is_flag=True)
def speakers(session, as_json):
    """List recording-local speaker IDs, names and available sample intervals."""
    state = session.snapshot()
    result = [{"speaker": key, "name": name, "passages": sum(row["speaker"] == key for row in state["segments"]),
               "samples": [{"start": start, "end": start + length}
                           for start, length in sample_windows(state["segments"], key)]}
              for key, name in state["speakers"].items()]
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for row in result:
            click.echo(f"{row['speaker']}\t{row['name'] or '(unnamed)'}\t{row['passages']} passages")


@cli.command()
@review_command
@click.option("--json", "as_json", is_flag=True)
@click.option("--reset-dismissed", is_flag=True, help="Reconsider previously rejected suggestions.")
def match(session, as_json, reset_dismissed):
    """Compare voices with remembered people; never assign names or retrain."""
    state = session.snapshot()
    if reset_dismissed:
        state["voice_dismissed"] = []
        session.update(state)
        state = session.snapshot()
    result = session.match_voice(state["revision"])
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for speaker, suggestion in result.get("suggestions", {}).items():
            names = ", ".join(choice["name"] for choice in suggestion["choices"]) or "no remembered match"
            click.echo(f"{speaker}\t{suggestion['status']}\t{names}")
        if not result["enrolled"]:
            click.echo("No remembered voices.")


@cli.command("name")
@review_command
@click.option("--speaker", required=True)
@click.option("--name", required=True, help="Confirmed display name; use an empty string to clear it.")
def name_speaker(session, speaker, name):
    """Assign or clear a transcript name without changing any voice profile."""
    state = session.snapshot()
    if speaker not in state["speakers"]:
        raise click.ClickException("Unknown speaker; run 'speakers' to list this recording's IDs.")
    state["speakers"][speaker] = name
    session.update(state)
    click.echo(f"{speaker}: {name or '(unnamed)'}")


@cli.command()
@review_command
@click.option("--speaker", required=True)
def dismiss(session, speaker):
    """Reject name suggestions for this recording's speaker; keep profiles intact."""
    state = session.snapshot()
    if speaker not in state["speakers"]:
        raise click.ClickException("Unknown speaker.")
    state["voice_dismissed"] = list(set(state.get("voice_dismissed", []) + [speaker]))
    session.update(state)
    click.echo(f"Suggestions dismissed for {speaker}.")


@cli.command()
@click.argument("profile_id")
def forget(profile_id):
    """Delete one local voice profile by its ID; keep saved transcript names."""
    memory = VoiceMemory()
    public = memory.public()
    if profile_id not in [profile["id"] for profile in public["profiles"]] + public["broken"]:
        raise click.ClickException("Unknown profile ID; run 'list' first.")
    memory.forget(profile_id)
    click.echo(f"Forgot profile {profile_id}; saved transcript names are unchanged.")


@cli.command()
@review_command
@click.option("--profile", type=click.Choice(["short", "middle", "long"]),
              help="Notes detail; defaults to the saved review setting.")
@click.option("--context", help="Meeting background; defaults to the saved review setting.")
@click.option("--meeting-date", type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Actual meeting date; defaults to the saved review setting.")
def notes(session, profile, context, meeting_date):
    """Generate LogSeq notes from saved speaker names and corrected passages.

    Reuses transcription. Requires OPENROUTER_API_KEY; notes generation incurs
    the normal model charge. Previous notes are retained if generation fails.
    Includes the last 30 days of local journals in the OpenRouter request.
    STT_JOURNAL_DIR overrides the journal folder; empty disables journal context.
    """
    state = session.snapshot()
    if profile is not None or context is not None or meeting_date is not None:
        if profile is not None:
            state["profile"] = profile
        if context is not None:
            state["context"] = context
        if meeting_date is not None:
            state["meeting_date"] = meeting_date.strftime("%Y-%m-%d")
        session.update(state)
    click.echo("Generating notes from the saved review (OpenRouter charges apply)...", err=True)
    session.generate(session.snapshot()["revision"])
    click.echo(f"Notes: {session.output / 'notes.md'}")


if __name__ == "__main__":
    cli()
