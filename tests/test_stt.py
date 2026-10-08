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

    def test_render_logseq_and_single_name_key(self):
        note = stt.Notes(
            overview=["Ruben agreed", "Ruben agreed"],
            actions=[stt.Action(task="Ruben will send [[proposal]]", owner="P1", due="Friday")],
            quotes=[stt.Quote(text="Ship it.", speaker="unknown")],
            facts_and_dates=["Budget: $500"],
        )
        output = stt.render_notes([note], {"P1": "Ruben"}, "2026-10-08")
        self.assertEqual(output.count("Ruben"), 1)
        self.assertEqual(output.count("P1 agreed"), 1)
        self.assertIn("- TODO P1 will send proposal — owner: P1; due: Friday", output)
        self.assertNotIn("[[", output)
        self.assertTrue(all(line.lstrip().startswith("- ") for line in output.splitlines()))

    def summary_response(self, content, finish="stop"):
        return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}]}

    def test_rejects_invented_quotes_attribution_and_truncated_notes(self):
        cases = [
            self.summary_response({"quotes": [{"text": "Invented quote"}]}),
            self.summary_response({"actions": [{"task": "Send it", "owner": "Alice"}]}),
            self.summary_response({}, "length"),
        ]
        for response in cases:
            with self.subTest(response=response), patch.object(stt, "request", return_value=response):
                with self.assertRaises(click.ClickException):
                    stt.extract_notes("Actual words", "fake-key", {"P1": "Ruben"}, "", None)

    def test_cli_full_flow_and_summary_retry(self):
        notes = {"actions": [{"task": "Send proposal", "owner": "unknown", "due": "Friday"}],
                 "facts_and_dates": ["Budget: $500"], "quotes": [{"text": "Ship it."}]}
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}), \
             patch.object(stt, "audio_parts", self.parts), patch.object(stt, "request") as request:
            request.side_effect = [{"text": "Send proposal by Friday."},
                                   {"text": "Budget: $500. Ship it."},
                                   click.ClickException("summary unavailable")]
            result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertTrue((self.output / "transcript.txt").exists())
            self.assertFalse((self.output / "notes.md").exists())
            request.side_effect = [self.summary_response(notes)]
            result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize", "--speaker", "Ruben"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(request.call_count, 4)
            output = (self.output / "notes.md").read_text()
            self.assertIn("TODO Send proposal — owner: unknown; due: Friday", output)
            self.assertIn('"Ship it." — unknown', output)

    def test_text_partition_preserves_all_content(self):
        original = ("A decision.\n" * 5000) + "Final action item."
        self.assertEqual("".join(stt.text_parts(original)), original)
        self.assertTrue(all(len(part) <= 24000 for part in stt.text_parts(original)))

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
