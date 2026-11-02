"""Local, opt-in voice enrollment and conservative name suggestions.

WeSpeaker ResNet34-LM (CC BY 4.0):
https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM
Features follow WeSpeaker: 16 kHz PCM, 80-bin Hamming fbank, mean normalization.
Similarity scores are cosine similarities, never calibrated probabilities.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import appdirs
import click
import numpy as np
import requests

from my_small_tools.stt import save

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

    def profile(self):
        with self.lock:
            if not self.profile_path.exists():
                return None
            profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
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

    def public(self):
        try:
            profile = self.profile()
        except (click.ClickException, ValueError, KeyError, TypeError, OSError, AttributeError):
            return {"enrolled": False, "name": "", "samples": 0,
                    "error": "The saved voice profile cannot be read; use This is me to replace it, or Forget my voice."}
        return {"enrolled": bool(profile), "name": profile["name"] if profile else "",
                "samples": len(profile["samples"]) if profile else 0}

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

    def enroll(self, audio, state, speaker, name):
        if (speaker not in state["speakers"] or not isinstance(name, str) or not name.strip()
                or len(name) > 200 or any(c in name for c in "\r\n")):
            raise click.ClickException("Choose your speaker and enter a single-line name.")
        with self.lock:
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
            save(self.profile_path, json.dumps(profile, ensure_ascii=False, indent=2))
            os.chmod(self.profile_path, 0o600)
            return self.public()

    def suggest(self, audio, state):
        with self.lock:
            profile = self.profile()
            if not profile:
                return {"enrolled": False, "suggestions": {}}
            centroid = unit(profile["centroid"])
            candidates = {}
            for speaker in state["speakers"]:
                vectors = self.vectors(audio, state, speaker)
                scores = [float(vector @ centroid) for vector in vectors]
                if scores:
                    candidates[speaker] = {"score": float(np.median(scores)), "samples": len(scores),
                                           "consistent": min(scores) >= profile["threshold"]}
            strong = [key for key, value in candidates.items() if value["samples"] >= 3
                      and value["consistent"] and profile["separated"]]
            for speaker, value in candidates.items():
                value["name"] = profile["name"]
                value["status"] = ("suggested" if len(strong) == 1 and speaker in strong else
                                   "unsure" if value["score"] >= MIN_SIMILARITY else "no_match")
                value.pop("consistent")
            for speaker in state["speakers"]:
                candidates.setdefault(speaker, {"name": profile["name"], "status": "unsure", "samples": 0, "score": None})
            return {**self.public(), "suggestions": candidates}

    def forget(self):
        with self.lock:
            self.profile_path.unlink(missing_ok=True)
        return self.public()
