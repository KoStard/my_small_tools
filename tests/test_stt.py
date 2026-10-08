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

    def sample_notes(self):
        return stt.Notes(topics=[stt.Topic(title="Proposal and budget", points=[
            stt.NotePoint(kind="action", text="Ruben will send [[proposal]]",
                          evidence="Ruben will send proposal by Friday.", owner="P1", due="Friday"),
            stt.NotePoint(kind="fact", text="Budget: $500", evidence="Budget: $500."),
            stt.NotePoint(kind="quote", text="Ship it.", evidence="Ship it."),
            stt.NotePoint(kind="detail", text="Ruben agreed", evidence="Ruben agreed."),
        ])])

    def sample_layout(self):
        return stt.Layout(topics=[stt.TopicLayout(
            title="Proposal and budget",
            point_ids=["N4", "N1", "N2", "N3"],
        )])

    def test_render_logseq_and_single_name_key(self):
        notes = self.sample_notes()
        inventory = stt.build_inventory([notes, notes])
        self.assertEqual(len(inventory), 4)
        output = stt.render_notes(self.sample_layout(), inventory, {"P1": "Ruben"}, "2026-10-08")
        self.assertEqual(output.count("Ruben"), 1)
        self.assertIn("  - TODO P1: P1 will send proposal — due: Friday", output)
        self.assertIn('- Proposal and budget\n  - P1 agreed', output)
        self.assertNotIn("Action items", output)
        self.assertNotIn("Important quotes", output)
        self.assertNotIn("[[", output)
        self.assertTrue(all(line.lstrip().startswith("- ") for line in output.splitlines()))

    def summary_response(self, content, finish="stop"):
        return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}]}

    def test_rejects_invented_quotes_attribution_and_truncated_notes(self):
        cases = [
            self.summary_response({"topics": [{"title": "Topic", "points": [
                {"kind": "quote", "text": "Invented quote", "evidence": "Actual words"}]}]}),
            self.summary_response({"topics": [{"title": "Topic", "points": [
                {"kind": "action", "text": "Send it", "owner": "Alice", "evidence": "Actual words"}]}]}),
            self.summary_response({"topics": [{"title": "Topic", "points": [
                {"kind": "fact", "text": "A fact", "evidence": "Invented evidence"}]}]}),
            self.summary_response({}, "length"),
        ]
        for response in cases:
            with self.subTest(response=response), patch.object(stt, "request", return_value=response):
                with self.assertRaises(click.ClickException):
                    stt.extract_notes("Actual words", "fake-key", {"P1": "Ruben"}, "", None)

    def test_notes_requests_do_not_set_output_token_limits(self):
        complete = self.summary_response(self.sample_notes().model_dump())
        with patch.object(stt, "request", return_value=complete) as request:
            stt.ask_json("fake-key", "Instructions", "Transcript", stt.Notes)
        payload = request.call_args.kwargs["json"]
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("max_output_tokens", payload)

    def test_provider_output_limit_reports_reason_without_accepting_partial_json(self):
        with patch.object(stt, "request", return_value=self.summary_response({}, "length")) as request:
            with self.assertRaisesRegex(click.ClickException, "finish_reason=length"):
                stt.ask_json("fake-key", "Instructions", "Transcript", stt.Notes)
        self.assertEqual(request.call_count, 1)

    def test_non_length_finish_reason_is_reported_without_retry(self):
        with patch.object(stt, "request", return_value=self.summary_response({}, "content_filter")) as request:
            with self.assertRaisesRegex(click.ClickException, "finish_reason=content_filter"):
                stt.ask_json("fake-key", "Instructions", "Transcript", stt.Notes)
        self.assertEqual(request.call_count, 1)

    def test_anonymous_statement_does_not_get_roster_owner(self):
        response = self.summary_response({"topics": [{"title": "Proposal", "points": [
            {"kind": "action", "text": "Send proposal", "evidence": "I will send it.", "owner": "P1"},
        ]}]})
        with patch.object(stt, "request", return_value=response):
            notes = stt.extract_notes("I will send it.", "fake-key", {"P1": "Ruben"}, "", None)
        self.assertEqual(notes.topics[0].points[0].owner, "unknown")

    def test_unknown_owner_cannot_leak_named_attribution_in_task_text(self):
        for task in ("P1 will send the deck", "Ruben will send the deck"):
            response = self.summary_response({"topics": [{"title": "Deck", "points": [
                {"kind": "action", "text": task, "evidence": "I will send the deck", "owner": "P1"},
            ]}]})
            with self.subTest(task=task), patch.object(stt, "request", return_value=response):
                with self.assertRaisesRegex(click.ClickException, "unsupported person reference"):
                    stt.extract_notes("I will send the deck", "fake-key", {"P1": "Ruben"}, "", None)

    def test_prior_context_is_not_extracted_again(self):
        response = self.summary_response({"topics": [{"title": "Proposal", "points": [
            {"kind": "action", "text": "Send proposal", "evidence": "Ruben will send it."},
        ]}]})
        with patch.object(stt, "request", return_value=response) as request:
            with self.assertRaisesRegex(click.ClickException, "unsupported evidence"):
                stt.extract_notes("Now discuss pricing.", "fake-key", {"P1": "Ruben"}, "", None,
                                  prior_context="Ruben will send it.")
            content = json.loads(request.call_args.kwargs["json"]["messages"][1]["content"])
            self.assertEqual(content["new_transcript"], "Now discuss pricing.")
            self.assertEqual(content["prior_transcript_context"], "Ruben will send it.")

    def test_cli_full_flow_and_summary_retry(self):
        notes = self.sample_notes().model_dump()
        layout = self.sample_layout().model_dump()
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}), \
             patch.object(stt, "audio_parts", self.parts), patch.object(stt, "request") as request:
            request.side_effect = [{"text": "Ruben agreed. Ruben will send proposal by Friday."},
                                   {"text": "Budget: $500. Ship it."},
                                   click.ClickException("summary unavailable")]
            result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertTrue((self.output / "transcript.txt").exists())
            self.assertFalse((self.output / "notes.md").exists())
            request.side_effect = [self.summary_response(notes), self.summary_response(layout)]
            result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize", "short", "--speaker", "Ruben"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(request.call_count, 5)
            output = (self.output / "notes.md").read_text()
            self.assertIn("TODO P1: P1 will send proposal — due: Friday", output)
            self.assertIn('"Ship it."', output)
            self.assertIn("short profile", request.call_args.kwargs["json"]["messages"][0]["content"])

    def test_layout_cannot_drop_protected_points(self):
        inventory = stt.build_inventory([self.sample_notes()])
        layout = self.sample_layout()
        for profile in ("short", "middle"):
            stt.validate_layout(layout, inventory, profile)
        stt.validate_layout(layout, inventory, "long")
        layout.topics[0].point_ids.remove("N4")
        with self.assertRaisesRegex(click.ClickException, "omitted required notes: N4"):
            stt.validate_layout(layout, inventory, "long")
        layout.topics[0].point_ids = ["N2", "N3"]
        for profile in stt.PROFILES:
            with self.assertRaisesRegex(click.ClickException, "omitted required notes"):
                stt.validate_layout(layout, inventory, profile)
        for identifiers in (["N1", "N2", "N3", "N99"], ["N1", "N2", "N3", "N1"]):
            layout.topics[0].point_ids = identifiers
            with self.assertRaisesRegex(click.ClickException, "invalid or duplicate"):
                stt.validate_layout(layout, inventory, "short")

    def test_profile_discussion_budgets_do_not_limit_actions(self):
        note = self.sample_notes()
        for index in range(7):
            note.topics[0].points.append(stt.NotePoint(
                kind="detail", text=f"Rationale {index}", evidence=f"Rationale {index}",
            ))
        inventory = stt.build_inventory([note])
        self.assertEqual(stt.detail_budget(inventory, "short"), 2)
        self.assertEqual(stt.detail_budget(inventory, "middle"), 5)
        self.assertEqual(stt.detail_budget(inventory, "long"), 8)
        layout = stt.Layout(topics=[stt.TopicLayout(title="Proposal", point_ids=list(inventory))])
        stt.validate_layout(layout, inventory, "long")
        for profile in ("short", "middle"):
            with self.assertRaisesRegex(click.ClickException, "optional discussion budget"):
                stt.validate_layout(layout, inventory, profile)
        layout.topics[0].point_ids = ["N1", "N2", "N3", "N4", "N5"]
        stt.validate_layout(layout, inventory, "short")

    def test_organizer_repairs_missing_action_and_merges_recurring_topic(self):
        first = self.sample_notes()
        second = stt.Notes(topics=[stt.Topic(title="Budget follow-up", points=[
            stt.NotePoint(kind="decision", text="Keep budget at $500", evidence="Keep budget at $500"),
        ])])
        inventory = stt.build_inventory([first, second])
        wrong = self.sample_layout().model_dump()
        wrong["topics"][0]["point_ids"] = ["N2", "N3", "N5"]
        correct = self.sample_layout().model_dump()
        correct["topics"][0]["point_ids"] = ["N1", "N2", "N3", "N5"]
        with patch.object(stt, "request", side_effect=[self.summary_response(wrong), self.summary_response(correct)]) as request:
            layout = stt.organize_notes(inventory, "fake-key", "short")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(layout.topics), 1)
        output = stt.render_notes(layout, inventory, {"P1": "Ruben"}, None)
        self.assertIn("TODO", output)
        self.assertIn("Decision: Keep budget at $500", output)
        self.assertNotIn("Budget follow-up", output)

    def test_summary_profiles_and_bare_flag_before_or_after_audio(self):
        for args, expected in [
            ([str(self.audio), "--summarize"], "middle"),
            (["--summarize", str(self.audio)], "middle"),
            (["--summarize", "--speaker", "Ruben", str(self.audio)], "middle"),
            ([str(self.audio), "--summarize", "short"], "short"),
            (["--summarize", "long", str(self.audio)], "long"),
            ([str(self.audio), "--summarize=MIDDLE"], "middle"),
        ]:
            with self.subTest(args=args), patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}), \
                 patch.object(stt, "transcribe", return_value="Actual words"), \
                 patch.object(stt, "extract_notes", return_value=self.sample_notes()), \
                 patch.object(stt, "organize_notes", return_value=self.sample_layout()) as organize:
                result = CliRunner().invoke(stt.main, args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(organize.call_args.args[2], expected)
        result = CliRunner().invoke(stt.main, [str(self.audio), "--summarize=tiny"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Invalid value", result.output)

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
