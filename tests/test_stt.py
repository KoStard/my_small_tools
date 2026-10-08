"""Offline checks: uv run python -m unittest discover -s tests -p 'test_stt.py'."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import click
from click.testing import CliRunner

from my_small_tools import stt


class STTTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audio = self.root / "meeting.wav"
        self.audio.write_bytes(b"audio-fixture")
        self.output = self.root / "meeting.stt"
        self.output.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def parts(self, source, directory):
        yield 0, 2, 0, source
        yield 1, 2, stt.CHUNK_SECONDS, source

    def test_failed_second_part_resumes_without_repeating_first(self):
        with patch.object(stt, "audio_parts", self.parts), patch.object(stt, "request") as request:
            request.side_effect = [{"text": "Ruben: Send the proposal by Friday."},
                                   click.ClickException("network failed")]
            with self.assertRaises(click.ClickException):
                stt.transcribe(self.audio, self.output, "fake-key", None, False)
            state = json.loads((self.output / "state.json").read_text())
            self.assertEqual(len(state["chunks"]), 1)
            request.side_effect = [{"text": 'The budget is $500. "Ship it."'}]
            text = stt.transcribe(self.audio, self.output, "fake-key", None, False)
            self.assertEqual(request.call_count, 3)
            self.assertIn("[02:00:00]", text)
            self.assertIn("$500", text)
            self.assertTrue(json.loads((self.output / "state.json").read_text())["complete"])

    def test_reuses_manual_edits_and_rejects_changed_source(self):
        with patch.object(stt, "audio_parts", self.parts), patch.object(stt, "request", return_value={"text": "Hello"}) as request:
            stt.transcribe(self.audio, self.output, "fake-key", None, False)
            (self.output / "transcript.txt").write_text("Corrected name and date")
            self.assertEqual(stt.transcribe(self.audio, self.output, "fake-key", None, False), "Corrected name and date")
            self.assertEqual(request.call_count, 2)
            self.audio.write_bytes(b"different-recording")
            with self.assertRaisesRegex(click.ClickException, "Recording or transcription settings changed"):
                stt.transcribe(self.audio, self.output, "fake-key", None, False)

    def test_completed_five_minute_transcript_is_reused(self):
        with patch.object(stt, "audio_parts", self.parts), patch.object(stt, "request", return_value={"text": "Hello"}) as request:
            stt.transcribe(self.audio, self.output, "fake-key", None, False)
            path = self.output / "state.json"
            state = json.loads(path.read_text())
            state["signature"]["chunk_seconds"] = 300
            state["signature"].pop("overlap_seconds", None)
            path.write_text(json.dumps(state))
            before = request.call_count
            self.assertIn("Hello", stt.transcribe(self.audio, self.output, "fake-key", None, False))
            self.assertEqual(request.call_count, before)

    def test_large_parts_use_base64_json_upload(self):
        with patch.object(stt, "audio_parts", self.parts), \
             patch.object(stt, "MAX_MULTIPART_BYTES", 2), \
             patch.object(stt, "request", return_value={"text": "Hello"}) as request:
            stt.transcribe(self.audio, self.output, "fake-key", "en", False)
            payload = request.call_args.kwargs["json"]
            self.assertEqual(payload["model"], stt.TRANSCRIPTION_MODEL)
            self.assertEqual(payload["language"], "en")
            self.assertEqual(payload["input_audio"]["format"], "wav")
            self.assertEqual(base64.b64decode(payload["input_audio"]["data"]), self.audio.read_bytes())

    def test_audio_under_two_hours_has_one_part(self):
        with patch.object(stt.shutil, "which", return_value="available"), \
             patch.object(stt, "run_media", return_value="7199"):
            parts = list(stt.audio_parts(self.audio, self.root))
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0][:3], (0, 1, 0))
        with patch.object(stt.shutil, "which", return_value="available"), \
             patch.object(stt, "run_media", return_value="7201"):
            parts = list(stt.audio_parts(self.audio, self.root))
            self.assertEqual(len(parts), 2)
            self.assertEqual(parts[1][:3], (1, 2, 7200))

    def notes_response(self, content="- Launch\n  - TODO Send the proposal by Friday.\n", finish="stop"):
        return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}

    def test_summary_uses_one_call_full_transcript_and_no_token_cap(self):
        transcript = "A long transcript. " * 5000
        with patch.object(stt, "request", return_value=self.notes_response()) as request:
            notes = stt.summarize_transcript(transcript, "fake-key", "middle", {"P1": "Ruben"}, "Weekly review", None)
        self.assertEqual(request.call_count, 1)
        payload = request.call_args.kwargs["json"]
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("response_format", payload)
        data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(data["transcript"], transcript)
        self.assertEqual(data["background"], "Weekly review")
        self.assertIn("TODO Send the proposal", notes)
        self.assertEqual(notes.count("Ruben"), 1)

    def test_plain_notes_are_not_rejected_by_schema_or_evidence_checks(self):
        content = '```markdown\n- Budget\n  - Revised budget: [[650 dollars]].\n  - "Ship it."\n```'
        with patch.object(stt, "request", return_value=self.notes_response(content)):
            notes = stt.summarize_transcript("Budget is $650. Ship it!", "fake-key", "short", {"P1": "name not set"}, "", None)
        self.assertIn("Revised budget: 650 dollars.", notes)
        self.assertNotIn("```", notes)
        self.assertNotIn("[[", notes)

    def test_provider_failures_keep_existing_notes(self):
        previous = "- Existing hand-edited notes\n"
        (self.output / "notes.md").write_text(previous)
        for response in (self.notes_response(finish="length"), self.notes_response("")):
            with self.subTest(response=response), patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}), \
                 patch.object(stt, "transcribe", return_value="Saved transcript"), \
                 patch.object(stt, "request", return_value=response):
                result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize"])
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual((self.output / "notes.md").read_text(), previous)
                self.assertIn("retained", result.output)

    def test_cli_summary_retry_reuses_transcript(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}), \
             patch.object(stt, "audio_parts", self.parts), patch.object(stt, "request") as request:
            request.side_effect = [{"text": "Send proposal by Friday."},
                                   {"text": "Budget: $500."},
                                   click.ClickException("summary unavailable")]
            result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertTrue((self.output / "transcript.txt").exists())
            request.side_effect = [self.notes_response()]
            result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize", "short"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(request.call_count, 4)
            self.assertTrue((self.output / "notes.md").exists())

    def test_profiles_and_bare_flag_before_or_after_audio(self):
        for args, profile in [
            ([str(self.audio), "--summarize"], "middle"),
            (["--summarize", str(self.audio)], "middle"),
            ([str(self.audio), "--summarize", "short"], "short"),
            (["--summarize", "long", str(self.audio)], "long"),
            ([str(self.audio), "--summarize=MIDDLE"], "middle"),
        ]:
            with self.subTest(args=args), patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}), \
                 patch.object(stt, "transcribe", return_value="Saved transcript"), \
                 patch.object(stt, "summarize_transcript", return_value="- Notes\n") as summarize:
                result = CliRunner().invoke(stt.main, args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(summarize.call_args.args[2], profile)
        result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize=tiny"])
        self.assertEqual(result.exit_code, 2)

    def test_missing_key_and_empty_audio(self):
        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(stt.main, [str(self.audio)])
            self.assertIn("Set OPENROUTER_API_KEY", result.output)
        self.audio.write_bytes(b"")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}):
            result = CliRunner().invoke(stt.main, [str(self.audio)])
            self.assertIn("Audio file is empty", result.output)


if __name__ == "__main__":
    unittest.main()
