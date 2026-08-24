import unittest

from aicut.db.store import Store
from aicut.intelligence.knowledge import ProductionKnowledge, consolidate
from aicut.intelligence.source_output import align_by_transcript, learn
from aicut.llm.mock import MockProducer
from aicut.models import Utterance


class SourceOutputTests(unittest.TestCase):
    """12.3 B: what a human kept, dropped and reordered is the core signal."""

    def setUp(self):
        self.source = [
            Utterance(0, 4, "hello everyone welcome to the stream"),
            Utterance(60, 64, "this boss keeps killing me"),
            Utterance(600, 604, "i finally beat the boss"),
            Utterance(900, 904, "anyway lets talk about lunch"),
        ]
        self.output = [
            Utterance(0, 4, "i finally beat the boss"),
            Utterance(5, 9, "this boss keeps killing me"),
        ]

    def test_alignment_finds_what_was_dropped(self):
        alignment = align_by_transcript(self.source, self.output)
        dropped = [s.text for s in alignment.spans if not s.kept]
        self.assertIn("hello everyone welcome to the stream", dropped)
        self.assertIn("anyway lets talk about lunch", dropped)

    def test_alignment_detects_reordering(self):
        self.assertTrue(align_by_transcript(self.source, self.output).reordered())

    def test_keep_ratio_is_measured(self):
        alignment = align_by_transcript(self.source, self.output)
        self.assertAlmostEqual(alignment.keep_ratio, 0.5)

    def test_learning_stores_the_pair_and_the_measurements(self):
        store = Store()
        alignment = align_by_transcript(self.source, self.output)
        analysis = learn(MockProducer(), store, alignment, source_ref="src.mkv", output_ref="out.mp4")
        self.assertEqual(analysis["measured"]["kept_spans"], 2)
        self.assertEqual(analysis["measured"]["dropped_spans"], 2)
        pairs = store.source_output_pairs()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["source_ref"], "src.mkv")
        store.close()


class KnowledgeTests(unittest.TestCase):
    def test_patterns_are_counted_not_copied(self):
        """4.6: the value is what several references share."""
        knowledge = consolidate([
            {"structure": {"opening": "show the result first"}},
            {"structure": {"opening": "show the result first"}},
            {"structure": {"opening": "chronological"}},
        ])
        top = knowledge.structure_patterns[0]
        self.assertEqual(top["support"], 2)
        self.assertAlmostEqual(top["share"], 0.667, places=2)

    def test_planner_view_carries_the_caveat(self):
        summary = ProductionKnowledge(sample_size=3).summary_for_planner()
        self.assertIn("not rules", summary["caveat"])

    def test_reference_rows_store_patterns_only(self):
        """4.6: there is nowhere in the schema to keep reference media."""
        store = Store()
        store.save_reference("vid1", "chan1", {"views": 10}, {"structure": {}})
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(tb_yt_reference)")}
        self.assertFalse({"media_path", "video_file", "download_path"} & columns)
        self.assertEqual(store.references()[0]["public_metrics"], {"views": 10})
        store.close()


class CalibrationMetricTests(unittest.TestCase):
    """17.3, including 9.4's requirement that pacing be scored against a human edit."""

    def test_pacing_recall_and_precision(self):
        from aicut.calibration.metrics import score_pacing

        # human kept 1 and 3; system kept 1 and 2
        score = score_pacing([True, True, False], [True, False, True])
        self.assertEqual(score.keep_recall, 0.5)
        self.assertEqual(score.cut_precision, 0.0)

    def test_perfect_agreement_scores_one(self):
        from aicut.calibration.metrics import score_pacing

        score = score_pacing([True, False, True], [True, False, True])
        self.assertEqual(score.accuracy, 1.0)
        self.assertEqual(score.f1, 1.0)

    def test_false_positive_rate_counts_promoted_junk(self):
        from aicut.calibration.metrics import score_content_discovery

        score = score_content_discovery([(0, 100), (500, 600)], [(0, 90)])
        self.assertEqual(score.matched, 1)
        self.assertEqual(score.false_positive_rate, 0.5)
        self.assertEqual(score.recall, 1.0)

    def test_sweep_marks_the_parameters_it_measured(self):
        from aicut.calibration import sweep
        from aicut.config import CalibrationProfile

        base = CalibrationProfile.load()
        self.assertTrue(base.is_provisional("pacing.keep_score_threshold"))
        result = sweep(
            base,
            {"pacing.keep_score_threshold": [0.3, 0.5, 0.7]},
            lambda p: 1.0 - abs(p.get_float("pacing.keep_score_threshold") - 0.5),
        )
        self.assertEqual(result.best_params["pacing.keep_score_threshold"], 0.5)
        self.assertFalse(result.profile.is_provisional("pacing.keep_score_threshold"))
        self.assertTrue(result.profile.is_provisional("pacing.trim_target_sec"))
        self.assertIsNotNone(result.profile.measured_at)


if __name__ == "__main__":
    unittest.main()
