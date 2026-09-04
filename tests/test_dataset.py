"""The 17.2 dataset and the 17.4 replay that scores against it.

This is the loop the whole project waits on: until a dataset exists every
threshold is a guess, and 17.5 makes the system say so on every run. So the
tooling that produces one has to work, and the sweep has to run without the
operator writing a harness of their own.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicut.calibration.dataset import Dataset, SilenceVerdict
from aicut.errors import AicutError
from aicut.intelligence.source_output import align_by_transcript
from aicut.media.audio import Silence
from aicut.models import Utterance


class DatasetTests(unittest.TestCase):
    def test_round_trip(self):
        dataset = Dataset(source_path="/s.mkv", channel_ref="mychannel")
        dataset.add_content(100, 340, "the boss fight")
        dataset.add_silence_verdict(50, 52, kept=True, note="stunned")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = dataset.save(Path(tmp) / "ds.json")
            reloaded = Dataset.load(path)
        self.assertEqual(reloaded.channel_ref, "mychannel")
        self.assertEqual(reloaded.content_spans[0].as_tuple(), (100.0, 340.0))
        self.assertTrue(reloaded.silence_verdicts[0].kept)

    def test_a_dataset_must_name_its_source(self):
        with self.assertRaises(AicutError):
            Dataset.from_dict({"schema_version": "1"})

    def test_a_future_schema_is_refused(self):
        with self.assertRaises(AicutError):
            Dataset.from_dict({"schema_version": "99", "source_path": "/s.mkv"})

    def test_backwards_spans_are_refused(self):
        dataset = Dataset(source_path="/s.mkv")
        with self.assertRaises(AicutError):
            dataset.add_content(300, 100)
        with self.assertRaises(AicutError):
            dataset.add_silence_verdict(5, 5, kept=True)

    def test_coverage_says_what_can_be_scored_yet(self):
        dataset = Dataset(source_path="/s.mkv")
        self.assertIn("nothing yet", dataset.coverage()["ready_for"][0])
        dataset.add_content(0, 60)
        self.assertIn("content discovery", dataset.coverage()["ready_for"][0])


class DeriveVerdictTests(unittest.TestCase):
    """12.3 B: the labels come out of an edit that already exists."""

    def setUp(self):
        # Source: four lines with a long pause between each.
        self.source = [
            Utterance(0, 4, "the tournament final begins"),
            Utterance(24, 28, "he almost lost it there"),
            Utterance(48, 52, "and he wins the tournament"),
            Utterance(72, 76, "lunch was a sandwich"),
        ]

    def _derive(self, output, silences):
        dataset = Dataset(source_path="/s.mkv")
        alignment = align_by_transcript(self.source, output)
        return dataset.derive_silence_verdicts(silences, alignment)

    def test_a_gap_the_editor_preserved_reads_as_kept(self):
        output = [
            Utterance(0, 4, "the tournament final begins"),
            Utterance(22, 26, "he almost lost it there"),      # 18s of the 20s gap survives
        ]
        verdict = self._derive(output, [Silence(5, 23)])[0]
        self.assertTrue(verdict.kept)
        self.assertIn("survived", verdict.note)

    def test_a_gap_the_editor_collapsed_reads_as_cut(self):
        output = [
            Utterance(0, 4, "the tournament final begins"),
            Utterance(4.5, 8.5, "he almost lost it there"),    # the pause is gone
        ]
        self.assertFalse(self._derive(output, [Silence(5, 23)])[0].kept)

    def test_a_pause_beside_dropped_material_is_cut_with_it(self):
        output = [Utterance(0, 4, "and he wins the tournament")]
        verdicts = self._derive(output, [Silence(53, 71)])
        self.assertFalse(verdicts[0].kept)
        self.assertIn("dropped", verdicts[0].note)

    def test_deriving_replaces_rather_than_appends(self):
        dataset = Dataset(source_path="/s.mkv")
        dataset.add_silence_verdict(0, 1, kept=True)
        alignment = align_by_transcript(self.source, self.source)
        dataset.derive_silence_verdicts([Silence(5, 23)], alignment)
        self.assertEqual(len(dataset.silence_verdicts), 1)
        self.assertNotEqual(dataset.silence_verdicts[0].start_sec, 0)


class ReplayHarnessTests(unittest.TestCase):
    """17.4 step 2: the sweep must run without the operator writing a harness."""

    def setUp(self):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.pipeline.context import RunContext, SignalBundle
        from aicut.pipeline.runner import Pipeline
        from aicut.pipeline.states import State
        from tests import fixtures

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self.store = Store(self.dir / "aicut.db")
        self.profile = CalibrationProfile.load()

        pipeline = Pipeline(self.store, self.profile, get_producer("mock"), workspace=self.dir)
        self.project = pipeline.submit("/fixture/stream.mkv")
        self.store.replace_utterances(self.project.project_id, fixtures.utterances())
        ctx = RunContext(
            project=self.project, store=self.store, profile=self.profile,
            producer=get_producer("mock"), workspace=self.dir, media=fixtures.media(),
            signals=SignalBundle(
                tension=fixtures.tension(), motion=fixtures.motion(),
                silences=fixtures.silences(), rms=[(float(t), -30.0) for t in range(0, 3600, 5)],
            ),
        )
        ctx.signals.save(ctx.signal_cache_path)
        result = pipeline.run(self.project, context=ctx, stop_after=State.UNDERSTANDING)
        self.assertIs(result.final_state, State.UNDERSTANDING)

        self.dataset = Dataset(source_path="/fixture/stream.mkv")
        self.dataset.add_content(0, 1900, "the boss fight, first mention to payoff")
        for silence, kept in zip(fixtures.silences(), (True, False, True)):
            self.dataset.add_silence_verdict(silence.start_sec, silence.end_sec, kept=kept)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _harness(self):
        from aicut.calibration import ReplayHarness

        return ReplayHarness(self.dataset, workspace=self.dir, project_id=self.project.project_id)

    def test_the_replay_answers_in_the_shape_the_metrics_score(self):
        harness = self._harness()
        try:
            system = harness.run(self.profile)
        finally:
            harness.close()
        self.assertEqual(len(system["pacing_keeps"]), len(self.dataset.silence_verdicts))
        self.assertTrue(all(isinstance(k, bool) for k in system["pacing_keeps"]))
        for span in system["content_spans"]:
            self.assertEqual(len(span), 2)

    def test_different_parameters_produce_different_verdicts(self):
        """A sweep over parameters that changed nothing would be theatre."""
        harness = self._harness()
        try:
            # Below every penalty the rule layer can apply, so nothing is cut.
            loose = harness.run(self.profile.with_overrides(
                {"pacing.keep_score_threshold": -1.0, "pacing.keep_max_sec": 120.0}, measured=[]))
            strict = harness.run(self.profile.with_overrides(
                {"pacing.keep_score_threshold": 0.95, "pacing.keep_max_sec": 0.1}, measured=[]))
        finally:
            harness.close()
        self.assertNotEqual(loose["pacing_keeps"], strict["pacing_keeps"])
        self.assertTrue(all(loose["pacing_keeps"]))
        self.assertFalse(any(strict["pacing_keeps"]))

    def test_a_sweep_runs_end_to_end_and_marks_what_it_measured(self):
        from aicut.calibration import build_evaluator, sweep

        harness = self._harness()
        try:
            result = sweep(
                self.profile,
                {"pacing.keep_score_threshold": [0.0, 0.5, 0.95]},
                build_evaluator(harness),
                channel_ref="mychannel",
            )
        finally:
            harness.close()

        self.assertEqual(len(result.trials), 3)
        self.assertIn("pacing.keep_score_threshold", result.best_params)
        self.assertFalse(result.profile.is_provisional("pacing.keep_score_threshold"))
        self.assertTrue(result.profile.is_provisional("silence.level_db"))
        self.assertIsNotNone(result.profile.measured_at)
        self.assertEqual(result.profile.name, "mychannel-calibrated")

    def test_the_harness_refuses_a_source_that_was_never_processed(self):
        from aicut.calibration import ReplayHarness

        stranger = Dataset(source_path="/never/seen.mkv")
        with self.assertRaises(AicutError) as raised:
            ReplayHarness(stranger, workspace=self.dir)
        self.assertIn("aicut run", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
