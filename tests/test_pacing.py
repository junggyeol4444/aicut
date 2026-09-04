import unittest

from aicut.analysis.pacing import PacingJudge, SilenceContext, build_silence_contexts, trim_target
from aicut.config import CalibrationProfile
from aicut.models import PacingMode
from tests import fixtures


class PacingTests(unittest.TestCase):
    """9.1: the same silence length must be able to mean opposite things."""

    def setUp(self):
        self.profile = CalibrationProfile.load()
        self.judge = PacingJudge(self.profile)

    def test_stunned_pause_after_a_loud_moment_is_kept(self):
        ctx = SilenceContext(start_sec=100, end_sec=101.5, preceding_tension=0.9, motion=0.005, scene_role="core")
        self.assertIs(self.judge.judge(ctx).mode, PacingMode.KEEP)

    def test_identical_length_silence_after_a_quiet_moment_is_not_kept(self):
        loud = SilenceContext(start_sec=100, end_sec=101.5, preceding_tension=0.9, motion=0.005)
        quiet = SilenceContext(start_sec=200, end_sec=201.5, preceding_tension=0.05, motion=0.4)
        self.assertIs(self.judge.judge(loud).mode, PacingMode.KEEP)
        self.assertIsNot(self.judge.judge(quiet).mode, PacingMode.KEEP)

    def test_long_dead_air_is_cut(self):
        ctx = SilenceContext(start_sec=2600, end_sec=2640, preceding_tension=0.05, motion=0.4)
        decision = self.judge.judge(ctx)
        self.assertIs(decision.mode, PacingMode.CUT)
        self.assertEqual(decision.trimmed_span, (2600, 2640))

    def test_handover_pause_survives(self):
        ctx = SilenceContext(
            start_sec=10, end_sec=11.0, preceding_tension=0.4,
            speaker_before="HOST", speaker_after="GUEST", motion=0.1,
        )
        self.assertTrue(ctx.is_speaker_handover)
        self.assertIs(self.judge.judge(ctx).mode, PacingMode.KEEP)

    def test_trim_keeps_the_head_of_the_silence(self):
        ctx = SilenceContext(start_sec=50, end_sec=52.0, preceding_tension=0.5, motion=0.2)
        decision = self.judge.judge(ctx)
        self.assertIs(decision.mode, PacingMode.TRIM)
        span = trim_target(decision, self.profile)
        keep = self.profile.get_float("pacing.trim_target_sec")
        self.assertEqual(span, (50 + keep, 52.0))

    def test_context_building_reads_the_signals_around_each_silence(self):
        contexts = build_silence_contexts(
            fixtures.silences(), fixtures.utterances(), fixtures.tension(),
            fixtures.motion(), self.profile, scene_role="core",
        )
        after_win = next(c for c in contexts if c.start_sec == 1835.5)
        self.assertGreater(after_win.preceding_tension, 0.8)
        self.assertLess(after_win.motion, 0.05)
        self.assertIs(self.judge.judge(after_win).mode, PacingMode.KEEP)

        away = next(c for c in contexts if c.start_sec == 2600.0)
        self.assertIs(self.judge.judge(away).mode, PacingMode.CUT)

    def test_producer_may_overrule_the_rule_layer(self):
        class Contrarian:
            name = "contrarian"

            def judge_pacing(self, payload):
                return {"pacing_mode": "KEEP", "reason": "the look on his face carries it"}

        judge = PacingJudge(self.profile, Contrarian())
        ctx = SilenceContext(start_sec=2600, end_sec=2640, preceding_tension=0.05, motion=0.4)
        decision = judge.judge(ctx)
        self.assertIs(decision.mode, PacingMode.KEEP)
        self.assertEqual(decision.decided_by, "producer")


if __name__ == "__main__":
    unittest.main()
