"""Resuming a project that stopped part way (16장).

The first pass is the expensive half of a run - one reasoning call per window,
around 180 of them on a six-hour broadcast. A failure in a later stage must not
send the bill for that a second time, so this counts the calls rather than
trusting the wiring.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import PipelineError
from aicut.llm.mock import MockProducer
from aicut.pipeline.context import RunContext, SignalBundle
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State
from tests import fixtures


class CountingProducer(MockProducer):
    """A mock that remembers how often each judgement was asked for."""

    def __init__(self):
        super().__init__()
        self.calls: dict[str, int] = {}

    def complete_json(self, task, system, payload):
        self.calls[task] = self.calls.get(task, 0) + 1
        return super().complete_json(task, system, payload)


class FailingAtDiscovery(CountingProducer):
    """Understanding succeeds; the stage after it blows up."""

    def __init__(self):
        super().__init__()
        self.armed = True

    def _task_discover_candidates(self, payload):
        if self.armed:
            raise RuntimeError("discovery exploded")
        return super()._task_discover_candidates(payload)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = Store(self.dir / "aicut.db")
        self.profile = CalibrationProfile.load()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _context(self, project, producer):
        ctx = RunContext(
            project=project, store=self.store, profile=self.profile, producer=producer,
            workspace=self.dir, media=fixtures.media(),
            signals=SignalBundle(
                tension=fixtures.tension(), motion=fixtures.motion(),
                silences=fixtures.silences(), speaker_reliability=1.0,
            ),
        )
        ctx.signals.save(ctx.signal_cache_path)
        return ctx

    def test_a_failure_after_understanding_does_not_cost_the_first_pass_again(self):
        producer = FailingAtDiscovery()
        pipeline = Pipeline(self.store, self.profile, producer, workspace=self.dir)
        project = pipeline.submit("/fixture/stream.mkv")
        self.store.replace_utterances(project.project_id, fixtures.utterances())

        first = pipeline.run(project, context=self._context(project, producer), render=False)
        self.assertIs(first.final_state, State.FAILED)
        window_calls = producer.calls["summarize_window"]
        self.assertGreater(window_calls, 1, "the fixture should cover several windows")
        self.assertTrue(self.store.windows(project.project_id), "understanding was not persisted")

        producer.armed = False
        second = pipeline.resume(
            project.project_id, context=self._context(project, producer), render=False,
        )
        self.assertIs(second.final_state, State.PLANNING)
        self.assertEqual(
            producer.calls["summarize_window"], window_calls,
            "the first pass was paid for twice",
        )
        self.assertEqual(second.report.get("resumed_from"), State.UNDERSTANDING.value)

    def test_resuming_reuses_the_stored_events(self):
        producer = CountingProducer()
        pipeline = Pipeline(self.store, self.profile, producer, workspace=self.dir)
        project = pipeline.submit("/fixture/stream.mkv")
        self.store.replace_utterances(project.project_id, fixtures.utterances())
        pipeline.run(project, context=self._context(project, producer),
                     stop_after=State.UNDERSTANDING, render=False)

        events_before = [e.event_id for e in self.store.events(project.project_id)]
        self.assertTrue(events_before)
        build_calls = producer.calls["build_events"]

        pipeline.resume(project.project_id, context=self._context(project, producer), render=False)
        self.assertEqual(producer.calls["build_events"], build_calls)
        self.assertEqual([e.event_id for e in self.store.events(project.project_id)], events_before)

    def test_the_stages_after_understanding_are_decided_again(self):
        """A re-tuned profile or a corrected verdict has to be able to change
        the outcome, so only the expensive half is reused."""
        producer = CountingProducer()
        pipeline = Pipeline(self.store, self.profile, producer, workspace=self.dir)
        project = pipeline.submit("/fixture/stream.mkv")
        self.store.replace_utterances(project.project_id, fixtures.utterances())
        pipeline.run(project, context=self._context(project, producer),
                     stop_after=State.UNDERSTANDING, render=False)

        discover_before = producer.calls.get("discover_candidates", 0)
        pipeline.resume(project.project_id, context=self._context(project, producer), render=False)
        self.assertGreater(producer.calls["discover_candidates"], discover_before)

    def test_resuming_a_project_with_nothing_understood_reads_it_from_the_start(self):
        producer = CountingProducer()
        pipeline = Pipeline(self.store, self.profile, producer, workspace=self.dir)
        project = pipeline.submit("/fixture/stream.mkv")
        self.store.replace_utterances(project.project_id, fixtures.utterances())

        result = pipeline.resume(
            project.project_id, context=self._context(project, producer), render=False,
        )
        self.assertIs(result.final_state, State.PLANNING)
        self.assertGreater(producer.calls["summarize_window"], 0)
        self.assertIsNone(result.report.get("resumed_from"))

    def test_resuming_an_unknown_project_is_refused(self):
        pipeline = Pipeline(self.store, self.profile, MockProducer(), workspace=self.dir)
        with self.assertRaises(PipelineError):
            pipeline.resume("not-a-project")


if __name__ == "__main__":
    unittest.main()
