"""Loopback-only speaker review; no browser credentials or third-party assets."""

import copy
from contextlib import contextmanager
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import click
from filelock import FileLock

from my_small_tools import stt

MODEL = "deepgram/nova-3"
OPTIONS = {"options": {"deepgram": {"diarize": True, "punctuate": True, "smart_format": True}}}


def timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def transcript(state):
    return "\n\n".join(
        f"[{timestamp(row['start'])}–{timestamp(row['end'])}] "
        f"{row['speaker']} ({state['speakers'][row['speaker']] or 'name not set'})\n{row['text']}"
        for row in state["segments"]
    ) + "\n"


def initial_state(raw, signature):
    if not isinstance(raw, dict):
        raise click.ClickException("Import a JSON object containing speaker-labeled segments.")
    rows = raw.get("segments")
    if (not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows)
            or not any(row.get("speaker") is not None for row in rows)):
        raise click.ClickException("This transcript has no speaker-labeled segments. Import a diarized JSON response.")
    labels, speakers, segments = {}, {}, []
    for index, row in enumerate(rows):
        if not isinstance(row.get("text"), str) or not all(
            isinstance(row.get(key), (int, float)) and not isinstance(row[key], bool)
            and math.isfinite(row[key]) for key in ("start", "end")
        ) or not 0 <= row["start"] < row["end"]:
            raise click.ClickException("Imported passages need text and valid start/end timestamps.")
        raw_id = str(row["speaker"]) if row.get("speaker") is not None else None
        if raw_id not in labels:
            identifier = f"P{len(labels) + 1}"
            labels[raw_id] = identifier
            speakers[identifier] = ""
        if segments and segments[-1]["speaker"] == labels[raw_id]:
            segments[-1]["text"] += " " + row["text"]
            segments[-1]["end"] = row["end"]
        else:
            segments.append({"id": f"segment-{index}", "start": row["start"], "end": row["end"],
                             "speaker": labels[raw_id], "text": row["text"]})
    duration = max(float(raw.get("duration") or 0), max(row["end"] for row in segments))
    if not math.isfinite(duration):
        raise click.ClickException("Imported duration must be finite.")
    state = {"version": 1, "signature": signature, "revision": 0, "duration": duration,
             "speakers": speakers, "segments": segments, "profile": "middle", "context": "",
             "meeting_date": "", "notes_revision": None}
    validate_edits(state, state)
    return state


def validate_edits(value, current):
    """Only editable fields cross the browser/server boundary."""
    if not isinstance(value, dict):
        raise ValueError("Expected a review object.")
    speakers, rows = value.get("speakers"), value.get("segments")
    if not isinstance(speakers, dict) or not 1 <= len(speakers) <= 100:
        raise ValueError("Keep between 1 and 100 speakers.")
    for key, name in speakers.items():
        if not re.fullmatch(r"P[1-9][0-9]{0,5}", key) or not isinstance(name, str) or len(name) > 200:
            raise ValueError("Invalid speaker name or identifier.")
        if "\n" in name or "\r" in name:
            raise ValueError("Speaker names must be a single line.")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError("Keep between 1 and 10000 passages.")
    seen = set()
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid passage.")
        identifier, start, end = row.get("id"), row.get("start"), row.get("end")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", identifier) or identifier in seen:
            raise ValueError("Passage IDs must be unique.")
        seen.add(identifier)
        if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) for t in (start, end)):
            raise ValueError("Passage times must be finite numbers.")
        if not 0 <= start < end <= current["duration"] + 1:
            raise ValueError("Passage times must be within the recording, with end after start.")
        if row.get("speaker") not in speakers or not isinstance(row.get("text"), str) or len(row["text"]) > 100000:
            raise ValueError("Invalid passage text or speaker.")
        cleaned.append({k: row[k] for k in ("id", "start", "end", "speaker", "text")})
    profile = value.get("profile", "middle")
    context, date = value.get("context", ""), value.get("meeting_date", "")
    if profile not in stt.PROFILES or not isinstance(context, str) or len(context) > 20000:
        raise ValueError("Invalid notes settings.")
    if not isinstance(date, str):
        raise ValueError("Invalid meeting date.")
    if date:
        from datetime import date as calendar_date
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("Invalid meeting date.")
        calendar_date.fromisoformat(date)
    dismissed = value.get("voice_dismissed", [])
    if not isinstance(dismissed, list) or any(not isinstance(key, str) or key not in speakers for key in dismissed):
        raise ValueError("Invalid voice suggestion feedback.")
    return {"speakers": speakers.copy(), "segments": cleaned, "profile": profile,
            "context": context, "meeting_date": date, "voice_dismissed": list(set(dismissed))}


class ReviewSession:
    def __init__(self, audio, output, imported=None, language=None, speakers=(), context="", meeting_date=None,
                 voice_memory=None):
        from my_small_tools.stt_voice import VoiceMemory
        self.voice_memory = voice_memory if voice_memory is not None else VoiceMemory()
        self.audio, self.output = audio.resolve(), output.resolve()
        self.lock = threading.Lock()
        with self.audio.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        signature = {"sha256": digest, "model": MODEL, "language": language, "provider": OPTIONS}
        self.path = self.output / "review.json"
        self.output.mkdir(parents=True, exist_ok=True)
        self.disk_lock = FileLock(str(self.output / "review.lock"), timeout=180)
        with self.disk_lock:
            self.load_or_create(signature, imported, language, speakers, context, meeting_date)

    def load_or_create(self, signature, imported, language, speakers, context, meeting_date):
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("signature") != signature:
                raise click.ClickException("Recording or review settings changed; choose another output directory.")
            validate_edits(self.state, self.state)
            self.persist()
        else:
            raw_path = self.output / "provider.json"
            signature_path = self.output / "recording.json"
            if raw_path.exists():
                if not signature_path.exists() or json.loads(signature_path.read_text()) != signature:
                    raise click.ClickException("Saved provider output belongs to different settings; choose another directory.")
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
            else:
                if any(path.name != "review.lock" for path in self.output.iterdir()):
                    raise click.ClickException("Choose an empty output directory for a new review.")
                raw = json.loads(imported.read_text(encoding="utf-8")) if imported else self.transcribe(language)
                # Store source identity first: a crash can never make raw output look reusable for another source.
                stt.save(signature_path, json.dumps(signature, ensure_ascii=False, indent=2))
                stt.save(raw_path, json.dumps(raw, ensure_ascii=False, indent=2))
            self.state = initial_state(raw, signature)
            for identifier, name in zip(self.state["speakers"], speakers):
                self.state["speakers"][identifier] = name.strip()
            self.state.update(context=context, meeting_date=meeting_date or "")
            self.persist()
        self.notes = (self.output / "notes.md").read_text(encoding="utf-8") if (self.output / "notes.md").exists() else ""

    def transcribe(self, language):
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise click.ClickException("Set OPENROUTER_API_KEY to transcribe, or use --transcript-json to import.")
        with tempfile.TemporaryDirectory(prefix="stt-review-") as temporary:
            part = self.audio
            if part.stat().st_size > stt.MAX_MULTIPART_BYTES or part.suffix.lower().lstrip(".") not in stt.FORMATS:
                if not shutil.which("ffmpeg"):
                    raise click.ClickException("Install ffmpeg to compress or convert this recording.")
                part = Path(temporary) / "recording.mp3"
                stt.run_media(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(self.audio),
                               "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(part)])
            if part.stat().st_size > stt.MAX_MULTIPART_BYTES:
                raise click.ClickException("Compressed recording exceeds 25 MB; review a shorter recording. Speaker IDs are not linked across chunks.")
            data = {"model": MODEL, "response_format": "verbose_json",
                    "timestamp_granularities[]": ["segment", "word"], "provider": json.dumps(OPTIONS)}
            if language:
                data["language"] = language
            click.echo("Transcribing with speaker detection...", err=True)
            with part.open("rb") as handle:
                return stt.request("audio/transcriptions", key, data=data, files={"file": (part.name, handle)})

    def persist(self):
        stt.save(self.output / "transcript.txt", transcript(self.state))
        stt.save(self.path, json.dumps(self.state, ensure_ascii=False, indent=2))

    @contextmanager
    def transaction(self):
        # The CLI and web server share this checkpoint; never overwrite edits
        # made by another process using an old in-memory revision.
        with self.lock, self.disk_lock:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("signature") != self.state["signature"]:
                raise Conflict("Review identity changed; reopen this recording.")
            validate_edits(saved, saved)
            self.state = saved
            notes_path = self.output / "notes.md"
            self.notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
            yield

    def snapshot(self):
        with self.transaction():
            return {**copy.deepcopy(self.state), "title": self.audio.stem, "notes": self.notes,
                    "voice": self.voice_memory.public()}

    def remember_voice(self, revision, speaker, name, profile_id="me"):
        with self.transaction():
            if revision != self.state["revision"]:
                raise Conflict("Save the latest corrections before remembering your voice.")
            result = self.voice_memory.enroll(self.audio, self.state, speaker, name, profile_id=profile_id)
            saved_name = result.get("saved_name", result["name"])
            self.state["speakers"][speaker] = saved_name
            self.state["voice_dismissed"] = []
            self.state["revision"] += 1
            self.persist()
            return {"voice": result, "revision": self.state["revision"], "speaker": speaker, "name": saved_name}

    def match_voice(self, revision):
        with self.transaction():
            if revision != self.state["revision"]:
                raise Conflict("Save corrections before checking your voice.")
            state = copy.deepcopy(self.state)
        result = self.voice_memory.suggest(self.audio, state)
        for speaker in state.get("voice_dismissed", []):
            result.get("suggestions", {}).pop(speaker, None)
        with self.transaction():
            if revision != self.state["revision"]:
                raise Conflict("Review changed during matching; check your voice again.")
        return result

    def update(self, value):
        with self.transaction():
            if value.get("revision") != self.state["revision"]:
                raise Conflict("This review changed in another window or CLI command; reload before saving.")
            edited = validate_edits(value, self.state)
            old = copy.deepcopy(self.state)
            self.state.update(edited)
            self.state["revision"] += 1
            try:
                self.persist()
            except OSError:
                self.state = old
                raise
            return self.state["revision"]

    def generate(self, revision):
        with self.transaction():
            if revision != self.state["revision"]:
                raise Conflict("Save the latest changes before generating notes.")
            state = copy.deepcopy(self.state)
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise click.ClickException("Set OPENROUTER_API_KEY in the server environment to generate notes.")
        if not any(row["text"].strip() for row in state["segments"]):
            raise ValueError("Add transcript text before generating notes.")
        speakers = {identifier: name.strip() or f"{identifier} (name not set)"
                    for identifier, name in state["speakers"].items()}
        notes = stt.summarize_transcript(transcript(state), key, state["profile"], speakers,
                                         state["context"], state["meeting_date"] or None)
        with self.transaction():
            if revision != self.state["revision"]:
                raise Conflict("Review changed during generation; generate again from the saved corrections.")
            stt.save(self.output / "notes.md", notes)
            self.state["notes_revision"] = revision
            stt.save(self.path, json.dumps(self.state, ensure_ascii=False, indent=2))
            self.notes = notes
        return notes


class Conflict(ValueError):
    pass


def make_server(session, port=0):
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Do not log the capability URL or private transcript.

        def reply(self, status, data, mime="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def route(self):
            if self.headers.get("Host") != server.address:
                self.reply(403, {"error": "Invalid local host."})
                return None
            path = urlsplit(self.path).path
            if not path.startswith(f"/{token}/"):
                self.reply(404, {"error": "Not found."})
                return None
            return path[len(token) + 2:]

        def do_GET(self):
            route = self.route()
            if route is None:
                return
            if route == "":
                self.reply(200, Path(__file__).with_name("stt_review.html").read_bytes(), "text/html; charset=utf-8")
            elif route == "session":
                self.reply(200, session.snapshot())
            elif route in {"transcript.txt", "notes.md"}:
                with session.transaction():
                    text = transcript(session.state) if route == "transcript.txt" else session.notes
                self.reply(200, text.encode(), "text/plain; charset=utf-8")
            elif route == "audio":
                self.send_audio()
            else:
                self.reply(404, {"error": "Not found."})

        def send_audio(self):
            size = session.audio.stat().st_size
            start, end, status = 0, size - 1, 200
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if not match or not any(match.groups()):
                    self.reply(416, {"error": "Invalid audio range."})
                    return
                first, last = match.groups()
                if first:
                    start, end = int(first), min(int(last), end) if last else end
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(session.audio.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with session.audio.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(remaining, 65536))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break  # Browsers cancel an earlier stream when seeking.
                    remaining -= len(chunk)

        def do_POST(self):
            route = self.route()
            if route is None:
                return
            if self.headers.get("Origin") != f"http://{server.address}":
                self.reply(403, {"error": "Only the local review page can change this session."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 5_000_000 or self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected a JSON request under 5 MB.")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object.")
                if route == "save":
                    self.reply(200, {"revision": session.update(value)})
                elif route == "notes":
                    self.reply(200, {"notes": session.generate(value.get("revision"))})
                elif route == "voice-enroll":
                    self.reply(200, session.remember_voice(value.get("revision"), value.get("speaker"), value.get("name", "Me"), value.get("profile_id", "me")))
                elif route == "voice-match":
                    self.reply(200, session.match_voice(value.get("revision")))
                elif route == "voice-forget":
                    self.reply(200, session.voice_memory.forget(value.get("profile_id", "me")))
                else:
                    self.reply(404, {"error": "Not found."})
            except Conflict as exc:
                self.reply(409, {"error": str(exc)})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Invalid review data or settings; check passages, names, and dates."})
            except click.ClickException as exc:
                self.reply(502, {"error": exc.format_message()})
            except Exception:
                self.reply(500, {"error": "Operation failed; saved corrections and previous notes are retained."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.address = f"127.0.0.1:{server.server_port}"
    server.review_url = f"http://{server.address}/{token}/"
    return server


def launch(audio, output, imported=None, language=None, speakers=(), context="", meeting_date=None,
           port=0, open_browser=True):
    session = ReviewSession(audio, output, imported, language, speakers, context, meeting_date)
    with make_server(session, port) as server:
        click.echo(f"Review: {server.review_url}")
        click.echo(f"Saved locally: {session.output}\nCtrl+C closes the review server.", err=True)
        if open_browser:
            webbrowser.open(server.review_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
