"""Review persistence, attribution, generation and local HTTP boundary checks."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import click
import requests
from click.testing import CliRunner

from my_small_tools import stt, stt_review as review
from my_small_tools.stt_voice import VoiceMemory


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audio = self.root / "meeting.wav"
        self.audio.write_bytes(bytes(range(128)))
        self.raw = {"duration": 12, "segments": [
            {"start": 0, "end": 4, "speaker": 0, "text": "Send the proposal."},
            {"start": 4, "end": 8, "speaker": 1, "text": "I will send it tomorrow."},
            {"start": 8, "end": 12, "speaker": 0, "text": "Thanks, Alice."},
        ]}
        self.imported = self.root / "provider.json"
        self.imported.write_text(json.dumps(self.raw))
        self.output = self.root / "review"
        self.session = review.ReviewSession(self.audio, self.output, self.imported,
                                            voice_memory=VoiceMemory(self.root / "voice"))

    def tearDown(self):
        self.temporary.cleanup()

    def test_corrections_and_split_survive_reopen_without_touching_raw_output(self):
        value = self.session.snapshot()
        value["speakers"] = {"P1": "Bob", "P2": "Alice"}
        # Correct a mistaken attribution and split a mixed-speaker passage.
        value["segments"][0]["speaker"] = "P2"
        value["segments"][1]["text"] = "I will send it"
        value["segments"][1]["end"] = 7
        value["segments"].insert(2, {"id": "split-new", "start": 7, "end": 8,
                                      "speaker": "P1", "text": "tomorrow."})
        self.session.update(value)
        with patch.object(stt, "request", side_effect=AssertionError("Must not retranscribe")):
            reopened = review.ReviewSession(self.audio, self.output)
        self.assertEqual(len(reopened.state["segments"]), 4)
        self.assertEqual(reopened.state["segments"][0]["speaker"], "P2")
        self.assertIn("P2 (Alice)", (self.output / "transcript.txt").read_text())
        self.assertEqual(json.loads((self.output / "provider.json").read_text()), self.raw)
        self.assertEqual(reopened.state["speakers"], value["speakers"])

    def test_stale_tab_cannot_overwrite_newer_corrections(self):
        stale = self.session.snapshot()
        self.session.update(stale)
        stale["speakers"]["P1"] = "Wrong name"
        with self.assertRaises(review.Conflict):
            self.session.update(stale)
        self.assertEqual(self.session.state["speakers"]["P1"], "")

    def test_invalid_edits_do_not_damage_checkpoint(self):
        before = self.session.path.read_bytes()
        for change in (lambda v: v["segments"][0].update(start=float("nan")),
                       lambda v: v["segments"][0].update(speaker="not-present"),
                       lambda v: v["segments"][0].update(end=9999),
                       lambda v: v["segments"][0].update(id="segment-1"),
                       lambda v: v.update(meeting_date="2026-02-30")):
            value = self.session.snapshot()
            change(value)
            with self.assertRaises(ValueError):
                self.session.update(value)
            self.assertEqual(self.session.path.read_bytes(), before)

    def test_notes_use_saved_names_text_and_corrected_assignments(self):
        value = self.session.snapshot()
        value["speakers"] = {"P1": "Bob", "P2": "Alice"}
        value["segments"][0].update(speaker="P2", text="Send the corrected proposal.")
        value.update(profile="long", meeting_date="2026-11-03", context="Review")
        revision = self.session.update(value)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
             patch.object(stt, "summarize_transcript", return_value="- Reviewed notes\n") as summarize:
            self.session.generate(revision)
        args = summarize.call_args.args
        self.assertIn("P2 (Alice)\nSend the corrected proposal.", args[0])
        self.assertEqual(args[2:], ("long", {"P1": "Bob", "P2": "Alice"}, "Review", "2026-11-03"))
        self.assertEqual(self.session.state["notes_revision"], revision)

    def test_generation_race_and_api_failure_retain_previous_notes(self):
        notes = self.output / "notes.md"
        notes.write_text("Previous notes")
        def concurrent_edit(*args):
            self.session.update(self.session.snapshot())
            return "Stale generated notes"
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
             patch.object(stt, "summarize_transcript", side_effect=concurrent_edit):
            with self.assertRaises(review.Conflict):
                self.session.generate(0)
        self.assertEqual(notes.read_text(), "Previous notes")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
             patch.object(stt, "summarize_transcript", side_effect=click.ClickException("API failed")):
            with self.assertRaises(click.ClickException):
                self.session.generate(self.session.state["revision"])
        self.assertEqual(notes.read_text(), "Previous notes")

    def test_different_recording_cannot_reuse_session(self):
        self.audio.write_bytes(b"different recording")
        with self.assertRaisesRegex(click.ClickException, "Recording or review settings changed"):
            review.ReviewSession(self.audio, self.output)

    def test_plain_transcript_does_not_invent_speakers(self):
        with self.assertRaisesRegex(click.ClickException, "no speaker-labeled"):
            review.initial_state({"text": "Hi", "segments": [{"start": 0, "end": 2, "text": "Hi"}]}, {})

    def test_new_review_transcribes_once_with_the_working_multipart_options(self):
        output = self.root / "new-review"
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
             patch.object(stt, "request", return_value=self.raw) as request:
            review.ReviewSession(self.audio, output)
            review.ReviewSession(self.audio, output)
        request.assert_called_once()
        self.assertEqual(request.call_args.args, ("audio/transcriptions", "test-key"))
        form = request.call_args.kwargs["data"]
        self.assertEqual(form["model"], "deepgram/nova-3")
        self.assertTrue(json.loads(form["provider"])["options"]["deepgram"]["diarize"])
        self.assertIn("file", request.call_args.kwargs["files"])

    def test_remembering_voice_assigns_only_the_confirmed_speaker_and_checks_revision(self):
        public = {"enrolled": True, "name": "Me", "samples": 3}
        with patch.object(self.session.voice_memory, "enroll", return_value=public) as enroll:
            result = self.session.remember_voice(0, "P2", "Me")
            with self.assertRaises(review.Conflict):
                self.session.remember_voice(0, "P1", "Me")
        enroll.assert_called_once()
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.session.state["speakers"], {"P1": "", "P2": "Me"})
        self.assertEqual(json.loads(self.session.path.read_text())["speakers"]["P2"], "Me")

    def test_rejected_suggestions_persist_and_profile_vectors_never_reach_browser(self):
        value = self.session.snapshot()
        value["voice_dismissed"] = ["P1"]
        revision = self.session.update(value)
        with patch.object(self.session.voice_memory, "suggest", return_value={
            "enrolled": True, "suggestions": {"P1": {"name": "Me", "status": "unsure"}}
        }):
            self.assertEqual(self.session.match_voice(revision)["suggestions"], {})
        reopened = review.ReviewSession(self.audio, self.output, voice_memory=self.session.voice_memory)
        self.assertEqual(reopened.state["voice_dismissed"], ["P1"])
        self.assertNotIn("centroid", reopened.snapshot()["voice"])

    def test_review_changes_during_matching_discard_stale_suggestions(self):
        def concurrent_edit(*args):
            self.session.update(self.session.snapshot())
            return {"enrolled": True, "suggestions": {}}
        with patch.object(self.session.voice_memory, "suggest", side_effect=concurrent_edit):
            with self.assertRaises(review.Conflict):
                self.session.match_voice(0)

    def test_local_http_audio_seeking_saves_and_origin_protection(self):
        with review.make_server(self.session) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = server.review_url
                self.assertEqual(requests.get(url, timeout=2).status_code, 200)
                audio = requests.get(url + "audio", headers={"Range": "bytes=10-19"}, timeout=2)
                self.assertEqual(audio.status_code, 206)
                self.assertEqual(audio.content, self.audio.read_bytes()[10:20])
                suffix = requests.get(url + "audio", headers={"Range": "bytes=-4"}, timeout=2)
                self.assertEqual(suffix.content, self.audio.read_bytes()[-4:])
                self.assertEqual(requests.get(url + "audio", headers={"Range": "bytes=999-"}, timeout=2).status_code, 416)
                self.assertEqual(requests.get(url + "../../provider.json", timeout=2).status_code, 404)
                self.assertEqual(requests.get(url + "session", headers={"Host": "attacker.example"}, timeout=2).status_code, 403)
                value = requests.get(url + "session", timeout=2).json()
                headers = {"Origin": "https://attacker.example"}
                self.assertEqual(requests.post(url + "save", json=value, headers=headers, timeout=2).status_code, 403)
                headers["Origin"] = f"http://{server.address}"
                value["speakers"]["P1"] = "Alice"
                saved = requests.post(url + "save", json=value, headers=headers, timeout=2)
                self.assertEqual(saved.json(), {"revision": 1})
                self.assertEqual(requests.post(url + "save", json=value, headers=headers, timeout=2).status_code, 409)
                self.assertIn("Alice", requests.get(url + "transcript.txt", timeout=2).text)
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
                     patch.object(stt, "summarize_transcript", return_value="- HTTP notes\n"):
                    response = requests.post(url + "notes", json={"revision": 1}, headers=headers, timeout=2)
                self.assertEqual(response.json()["notes"], "- HTTP notes\n")
            finally:
                server.shutdown()
                thread.join()

    def test_review_import_cli_needs_no_key_and_keeps_plain_stt_separate(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), patch.object(review, "launch") as launch:
            result = CliRunner().invoke(stt.main, [str(self.audio), "--review", "--transcript-json", str(self.imported)])
        self.assertEqual(result.exit_code, 0, result.output)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[2], self.imported)


if __name__ == "__main__":
    unittest.main()
