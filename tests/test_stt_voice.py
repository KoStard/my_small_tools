"""Voice enrollment, rejection and ambiguity checks without network/model downloads."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import click
import numpy as np

from my_small_tools.stt_voice import VoiceMemory, MODEL_ID, sample_windows, unit


def vector(first, second=0):
    value = np.zeros(256, dtype=np.float32)
    value[:2] = first, second
    return unit(value)


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.memory = VoiceMemory(self.root / "voice")
        self.state = {"speakers": {"P1": "", "P2": ""}, "segments": [],
                      "signature": {"sha256": "recording-one"}}

    def tearDown(self):
        self.temporary.cleanup()

    def enroll(self):
        own = [vector(1, 0), vector(1, .1), vector(1, -.1)]
        with patch.object(self.memory, "vectors", side_effect=lambda a, s, who: own if who == "P2" else [vector(0, 1)]):
            self.memory.enroll(self.root / "audio.wav", self.state, "P2", "Me")

    def test_enrollment_is_private_and_contains_no_audio(self):
        self.enroll()
        raw = json.loads(self.memory.profile_path.read_text())
        self.assertEqual(raw["model"], MODEL_ID)
        self.assertGreaterEqual(raw["threshold"], .5)
        self.assertEqual(raw["name"], "Me")
        self.assertEqual(len(raw["samples"]), 3)
        self.assertEqual(self.memory.profile_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.memory.public()["samples"], 3)
        self.assertNotIn("centroid", self.memory.public())
        self.assertEqual(list(self.memory.directory.iterdir()), [self.memory.profile_path])

    def test_future_recording_can_swap_speaker_ids_without_renaming_anyone(self):
        self.enroll()
        self.state["signature"]["sha256"] = "new-recording"
        with patch.object(self.memory, "vectors", side_effect=lambda a, s, who: [vector(1, 0)] * 3 if who == "P1" else [vector(0, 1)] * 3):
            result = self.memory.suggest(self.root / "different.wav", self.state)
        self.assertEqual(result["suggestions"]["P1"]["status"], "suggested")
        self.assertEqual(result["suggestions"]["P2"]["status"], "no_match")
        self.assertEqual(self.state["speakers"], {"P1": "", "P2": ""})

    def test_ambiguous_voices_and_short_evidence_ask_instead_of_asserting(self):
        self.enroll()
        for count in (1, 3):
            with patch.object(self.memory, "vectors", return_value=[vector(1, 0)] * count):
                result = self.memory.suggest(self.root / "different.wav", self.state)
            self.assertTrue(all(item["status"] == "unsure" for item in result["suggestions"].values()))

    def test_mixed_cluster_and_borderline_similarity_are_uncertain(self):
        self.enroll()
        for samples in ([vector(1), vector(1), vector(0, 1)], [vector(.6, .8)] * 3):
            # The first case disagrees within a diarization cluster; the second
            # is above the floor but below an enrollment-calibrated threshold.
            profile = json.loads(self.memory.profile_path.read_text())
            profile["threshold"] = .75
            self.memory.profile_path.write_text(json.dumps(profile))
            with patch.object(self.memory, "vectors", side_effect=lambda a, s, who: samples if who == "P2" else [vector(0, 1)] * 3):
                result = self.memory.suggest(self.root / "different.wav", self.state)
            self.assertEqual(result["suggestions"]["P2"]["status"], "unsure")

    def test_insufficient_or_inconsistent_enrollment_retains_existing_profile(self):
        self.enroll()
        before = self.memory.profile_path.read_bytes()
        for vectors in ([vector(1)], [vector(1), vector(0, 1), vector(-1)]):
            with patch.object(self.memory, "vectors", return_value=vectors):
                with self.assertRaises(click.ClickException):
                    self.memory.enroll(self.root / "different.wav", self.state, "P2", "Me")
            self.assertEqual(self.memory.profile_path.read_bytes(), before)

    def test_samples_are_disjoint_and_short_turns_are_not_used(self):
        rows = [{"speaker": "P2", "start": 0, "end": 31}, {"speaker": "P2", "start": 40, "end": 42}]
        windows = sample_windows(rows, "P2")
        self.assertGreaterEqual(len(windows), 3)
        for i, (start, length) in enumerate(windows):
            self.assertLessEqual(start + length, 31)
            for other, span in windows[i + 1:]:
                self.assertTrue(start + length <= other or other + span <= start)

    def test_forgetting_removes_signature_and_disables_suggestions(self):
        self.enroll()
        self.memory.forget()
        self.assertFalse(self.memory.profile_path.exists())
        with patch.object(self.memory, "vectors", side_effect=AssertionError("No matching without enrollment")):
            result = self.memory.suggest(self.root / "audio.wav", self.state)
        self.assertFalse(result["enrolled"])

    def test_corrupt_profile_can_be_forgotten_without_blocking_the_review(self):
        self.memory.directory.mkdir()
        self.memory.profile_path.write_text("broken json")
        public = self.memory.public()
        self.assertFalse(public["enrolled"])
        self.assertIn("error", public)
        self.assertFalse(self.memory.forget()["enrolled"])

    def enroll_georg(self):
        with patch.object(self.memory, "vectors", side_effect=lambda a, s, who: [vector(0, 1)] * 3 if who == "P1" else [vector(1)] * 3):
            return self.memory.enroll(self.root / "audio.wav", self.state, "P1", "Georg Maerz", profile_id=None)

    def test_other_people_are_separate_private_profiles_without_replacing_me(self):
        self.enroll()
        own_before = self.memory.profile_path.read_bytes()
        result = self.enroll_georg()
        self.assertEqual(result["saved_name"], "Georg Maerz")
        self.assertEqual(self.memory.profile_path.read_bytes(), own_before)
        path = self.memory.path_for(result["saved_id"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual({p["name"] for p in self.memory.public()["profiles"]}, {"Me", "Georg Maerz"})
        self.memory.forget(result["saved_id"])
        self.assertFalse(path.exists())
        self.assertEqual(self.memory.profile_path.read_bytes(), own_before)

    def test_two_distinct_people_match_after_speaker_ids_change(self):
        self.enroll()
        self.enroll_georg()
        with patch.object(self.memory, "vectors", side_effect=lambda a, s, who: [vector(1)] * 3 if who == "P1" else [vector(0, 1)] * 3):
            suggestions = self.memory.suggest(self.root / "new.wav", self.state)["suggestions"]
        self.assertEqual(suggestions["P1"]["name"], "Me")
        self.assertEqual(suggestions["P2"]["name"], "Georg Maerz")
        self.assertTrue(all(s["status"] == "suggested" for s in suggestions.values()))

    def test_similar_saved_people_are_offered_as_choices_instead_of_picking_one(self):
        self.enroll()
        with patch.object(self.memory, "vectors", side_effect=lambda a, s, who: [vector(1)] * 3 if who == "P1" else [vector(0, 1)] * 3):
            self.memory.enroll(self.root / "audio.wav", self.state, "P1", "Similar voice", profile_id=None)
        with patch.object(self.memory, "vectors", side_effect=lambda a, s, who: [vector(1)] * 3 if who == "P2" else [vector(0, 1)] * 3):
            suggestion = self.memory.suggest(self.root / "new.wav", self.state)["suggestions"]["P2"]
        self.assertEqual(suggestion["status"], "unsure")
        self.assertEqual({choice["name"] for choice in suggestion["choices"]}, {"Me", "Similar voice"})

    def test_duplicate_names_and_unknown_update_ids_cannot_overwrite_profiles(self):
        self.enroll()
        before = self.memory.profile_path.read_bytes()
        for profile_id, name in ((None, "Me"), ("a" * 32, "Unknown"), ("../me", "Bad path")):
            with patch.object(self.memory, "vectors", side_effect=AssertionError("Do not extract invalid enrollment")):
                with self.assertRaises(click.ClickException):
                    self.memory.enroll(self.root / "audio.wav", self.state, "P1", name, profile_id=profile_id)
        self.assertEqual(self.memory.profile_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
