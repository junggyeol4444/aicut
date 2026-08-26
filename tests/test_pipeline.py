"""End-to-end pipeline tests, run offline against the synthetic broadcast."""

import tempfile
import unittest
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.llm import get_producer
from aicut.llm.mock import MockProducer
from aicut.models import Decision, PacingMode
from aicut.pipeline.context import RunContext, SignalBundle
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State, can_transition
from aicut.render.editplan import EditPlan
from aicut.render.timeline import Timeline
from tests import fixtures


class PipelineHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self._tmp.name)
        self.store = Store(self.workspace / "aicut.db")
        self.profile = CalibrationProfile.load()
        self.pipeline = Pipeline(
            self.store, self.profile, get_producer("mock"), workspace=self.workspace
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def seed(self, *, producer=None, utterances=None, length_hint=None) -> RunContext:
        """A project whose measurement stage is already done (no ffmpeg needed)."""
        project = self.pipeline.submit("/fixture/stream.mkv", length_hint_sec=length_hint)
        self.store.replace_utterances(
            project.project_id, fixtures.utterances() if utterances is None else utterances
        )
        ctx = RunContext(
            project=project,
            store=self.store,
            profile=self.profile,
            producer=producer or self.pipeline.producer,
            workspace=self.workspace,
            media=fixtures.media(),
            signals=SignalBundle(
                tension=fixtures.tension(),
                motion=fixtures.motion(),
                silences=fixtures.silences(),
                speaker_reliability=1.0,
            ),
        )
        ctx.signals.save(ctx.signal_cache_path)
        return ctx


class StateMachineTests(unittest.TestCase):
    def test_review_is_the_only_route_to_published(self):
        """11.3: the gate is structural, not a convention."""
        for state in State:
            if state is State.REVIEW_PENDING or state is State.RETRY_QUEUED:
                continue
            self.assertFalse(can_transition(state, State.PUBLISHED), f"{state} reached PUBLISHED directly")

    def test_no_content_is_terminal_and_not_a_failure(self):
        self.assertFalse(can_transition(State.NO_CONTENT, State.FAILED))
        self.assertTrue(can_transition(State.DISCOVERING, State.NO_CONTENT))
        self.assertTrue(can_transition(State.EVALUATING, State.NO_CONTENT))

    def test_a_failure_can_resume_at_the_render_stage(self):
        """16장: a failed render must not cost the edit plan."""
        self.assertTrue(can_transition(State.FAILED, State.RENDERING))


class RunTests(PipelineHarness):
    def test_run_to_planning_produces_readable_edit_plans(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)

        self.assertIs(result.final_state, State.PLANNING)
        self.assertTrue(result.episodes)
        plans = list((self.workspace / ctx.project.project_id / "plans").glob("*.json"))
        self.assertEqual(len(plans), len(result.episodes))

        plan = EditPlan.load(plans[0])
        # submit() resolves the source, so the recorded path is absolute and
        # platform-shaped - D:\fixture\stream.mkv on Windows.
        self.assertTrue(Path(plan.source_path).is_absolute())
        self.assertEqual(Path(plan.source_path).name, "stream.mkv")
        self.assertTrue(plan.cuts)
        self.assertEqual(
            [c.sequence_order for c in plan.cuts], list(range(len(plan.cuts))),
            "cut order must be dense and explicit",
        )

    def test_the_whole_broadcast_is_covered_by_the_first_pass(self):
        """5.1: no second of the source may be skipped."""
        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        windows = self.store.windows(ctx.project.project_id)
        self.assertTrue(windows)
        self.assertAlmostEqual(windows[0].start_sec, 0.0)
        self.assertAlmostEqual(windows[-1].end_sec, fixtures.DURATION)
        for earlier, later in zip(windows, windows[1:]):
            self.assertAlmostEqual(earlier.end_sec, later.start_sec, msg="a gap opened between windows")

    def test_events_link_moments_that_sit_far_apart(self):
        """5.4: the boss story is mentioned at 00:30 and paid off at 30:00."""
        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        events = self.store.events(ctx.project.project_id)
        self.assertTrue(events)
        spans = [event.span() for event in events]
        self.assertTrue(
            any(end - start > 1500 for start, end in spans),
            f"no event spans a long stretch of the broadcast: {spans}",
        )

    def test_the_report_records_refusals_and_provisional_parameters(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        report = result.report
        self.assertIn("decisions", report)
        self.assertIn("provisional_parameters_used", report)
        self.assertTrue(report["provisional_parameters_used"], "unmeasured values must be declared (17.5)")
        self.assertIn("17.5", report["warning"])
        self.assertTrue((self.workspace / ctx.project.project_id / "report.json").exists())

    def test_an_empty_broadcast_ends_in_no_content_not_failure(self):
        """1.3 / 16장: producing nothing is a correct answer."""
        ctx = self.seed(utterances=[])
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        self.assertIs(result.final_state, State.NO_CONTENT)
        self.assertTrue(result.produced_nothing)
        self.assertTrue(result.report["no_content_reason"])
        self.assertEqual(self.store.get_project(ctx.project.project_id).status, "NO_CONTENT")

    def test_a_producer_that_rejects_everything_yields_no_episodes(self):
        class Refuser(MockProducer):
            name = "refuser"

            def _task_evaluate_candidates(self, payload):
                return [
                    {"candidate_id": c["candidate_id"], "decision": "reject",
                     "reason": "not enough happens here to carry a video", "combine_with": []}
                    for c in payload.get("candidates", [])
                ]

        ctx = self.seed(producer=Refuser())
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        self.assertIs(result.final_state, State.NO_CONTENT)
        self.assertTrue(result.report["rejections"])
        self.assertIn("not enough happens", result.report["rejections"][0]["reason"])

    def test_pacing_decisions_reach_the_plan_with_their_reasons(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        cuts = [c for e in result.episodes for c in e.timeline]
        self.assertTrue(cuts)
        for cut in cuts:
            self.assertIsInstance(cut.pacing_mode, PacingMode)
            self.assertTrue(cut.pacing_reason)
            for start, end in cut.remove_spans:
                self.assertGreaterEqual(start, cut.source_start_sec)
                self.assertLessEqual(end, cut.source_end_sec)

    def test_subtitles_land_on_the_output_clock(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        episode = next(e for e in result.episodes if e.subtitles)
        timeline = Timeline.from_cuts(episode.timeline)
        for line in episode.subtitles:
            self.assertGreaterEqual(line.start_sec, 0.0)
            self.assertLessEqual(line.end_sec, timeline.duration + 0.01)

    def test_length_hint_is_a_hint_and_deviation_is_explained(self):
        """2.6: the slider never constrains the edit, but the report says so."""
        ctx = self.seed(length_hint=45.0)
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        deviations = result.report.get("length_deviations", [])
        if deviations:
            self.assertTrue(deviations[0]["reason"])
        else:
            for episode in result.episodes:
                self.assertLess(abs(episode.planned_duration_sec - 45.0) / 45.0, 0.25)

    def test_state_log_records_every_transition(self):
        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        states = [row["state"] for row in self.store.state_log(ctx.project.project_id)]
        self.assertEqual(
            states[:5],
            [State.PARSING.value, State.UNDERSTANDING.value, State.DISCOVERING.value,
             State.EVALUATING.value, State.PLANNING.value],
        )


class ReviewGateTests(PipelineHarness):
    def test_publishing_refuses_an_unapproved_episode(self):
        from aicut.pipeline import publishing, review

        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        episode = result.episodes[0]
        episode.output_mp4_path = "/fake/out.mp4"
        episode.metadata = {"youtube": {"video_id": "abc123"}}
        self.store.save_episode(episode)

        with self.assertRaises(PermissionError):
            publishing.publish_approved(ctx, episode, client=None)

        review.approve(ctx, episode.episode_id, reviewer="tester", note="fine")
        approved = self.store.get_episode(episode.episode_id)
        self.assertEqual(approved.review_status, "approved")
        self.assertFalse(approved.metadata["review"]["auto"])

    def test_human_verdicts_on_candidates_are_kept_for_learning(self):
        from aicut.pipeline import review

        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        candidates = self.store.candidates(ctx.project.project_id)
        produced = [c for c in candidates if c.decision is Decision.PRODUCE]
        self.assertTrue(produced)

        review.record_candidate_verdict(ctx, produced[0].candidate_id, "disagree", "I'd never cut this")
        rate = review.agreement_rate(ctx)
        self.assertEqual(rate["reviewed"], 1)
        self.assertEqual(rate["agreement"], 0.0)
        self.assertEqual(rate["false_positive_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
