"""Voice CLI operations use the same durable profiles and review as the UI."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from click.testing import CliRunner

from my_small_tools import stt, stt_voice
from my_small_tools.stt_review import ReviewSession


class VoiceCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audio = self.root / "meeting.wav"
        self.audio.write_bytes(b"test audio")
        self.imported = self.root / "provider.json"
        self.imported.write_text(json.dumps({"duration": 40, "segments": [
            {"speaker": 0, "start": 0, "end": 20, "text": "One voice."},
            {"speaker": 1, "start": 20, "end": 40, "text": "Another voice."}]}))
        self.output = self.root / "review"
        self.memory = stt_voice.VoiceMemory(self.root / "voices")
        self.session = ReviewSession(self.audio, self.output, self.imported, voice_memory=self.memory)
        self.runner = CliRunner()

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, args, recording=True):
        with patch.object(stt_voice, "VoiceMemory", return_value=self.memory):
            return self.runner.invoke(stt_voice.cli, args + ([str(self.audio), "-o", str(self.output)] if recording else []))

    def test_remember_match_name_dismiss_and_forget_without_opening_browser(self):
        def vectors(audio, state, speaker):
            value = np.zeros(256, dtype=np.float32)
            value[0 if speaker == "P1" else 1] = 1
            return [value] * 3
        with patch.object(self.memory, "vectors", side_effect=vectors):
            result = self.invoke(["remember", "--speaker", "P1", "--name", "Georg Maerz"])
            self.assertEqual(result.exit_code, 0, result.output)
            listed = self.invoke(["list", "--json"], recording=False)
            public = json.loads(listed.output)
            self.assertNotIn("centroid", listed.output)
            identifier = public["profiles"][0]["id"]
            before = self.memory.path_for(identifier).read_bytes()
            result = self.invoke(["match", "--json"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(json.loads(result.output)["suggestions"]["P1"]["name"], "Georg Maerz")
            result = self.invoke(["name", "--speaker", "P2", "--name", "Alice"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(self.memory.path_for(identifier).read_bytes(), before)
            self.assertEqual(self.session.snapshot()["speakers"]["P2"], "Alice")
            self.assertEqual(self.invoke(["dismiss", "--speaker", "P1"]).exit_code, 0)
            result = self.invoke(["match", "--json"])
            self.assertNotIn("P1", json.loads(result.output)["suggestions"])
            result = self.invoke(["match", "--reset-dismissed", "--json"])
            self.assertIn("P1", json.loads(result.output)["suggestions"])
        result = self.invoke(["forget", identifier], recording=False)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(self.memory.path_for(identifier).exists())
        self.assertEqual(self.session.snapshot()["speakers"]["P1"], "Georg Maerz")

    def test_speaker_listing_and_missing_review_do_not_call_transcription(self):
        with patch.object(stt, "request", side_effect=AssertionError("No transcription")):
            result = self.invoke(["speakers", "--json"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual([row["speaker"] for row in json.loads(result.output)], ["P1", "P2"])
            result = self.runner.invoke(stt_voice.cli, ["match", str(self.audio)])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("No saved review", result.output)

    def test_import_initializes_without_api_key_and_unknown_speaker_is_rejected(self):
        output = self.root / "imported-review"
        with patch.object(stt_voice, "VoiceMemory", return_value=self.memory):
            result = self.runner.invoke(stt_voice.cli, ["speakers", str(self.audio), "-o", str(output),
                                                       "--transcript-json", str(self.imported), "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        before = self.session.path.read_bytes()
        result = self.invoke(["name", "--speaker", "P99", "--name", "Unknown"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.session.path.read_bytes(), before)

    def test_notes_use_confirmed_names_and_corrected_text_without_retranscription(self):
        state = self.session.snapshot()
        state["speakers"] = {"P1": "Georg Maerz", "P2": "Ruben Kostandyan"}
        state["segments"][0]["text"] = "Corrected discussion."
        self.session.update(state)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
             patch.object(stt, "summarize_transcript", return_value="- Meeting notes\n") as summarize, \
             patch.object(stt, "request", side_effect=AssertionError("No transcription")):
            result = self.invoke(["notes", "--profile", "long"])
        self.assertEqual(result.exit_code, 0, result.output)
        args = summarize.call_args.args
        self.assertIn("P1 (Georg Maerz)\nCorrected discussion.", args[0])
        self.assertEqual(args[2], "long")
        self.assertEqual(args[3], state["speakers"])
        self.assertEqual((self.output / "notes.md").read_text(), "- Meeting notes\n")


if __name__ == "__main__":
    unittest.main()
