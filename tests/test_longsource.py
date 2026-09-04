"""Long-source handling (16장) and the face signal (5.3, 11.1)."""

import tempfile
import unittest
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.llm.mock import MockProducer
from aicut.media.faces import FaceReading, expression_change, face_ratio_lookup
from aicut.models import Project, SituationLabel, Utterance
from aicut.pipeline import understanding
from aicut.pipeline.context import RunContext, SignalBundle
from tests import fixtures


class ChunkedEventGraphTests(unittest.TestCase):
    """A 12-hour source is folded in chunks, then merged - not truncated."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self._tmp.name)
        self.store = Store(self.workspace / "db.sqlite")
        self.profile = CalibrationProfile.load()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _context(self, duration: float, utterances):
        project = self.store.create_project(
            Project(file_path="/fixture/long.mkv", duration_sec=duration)
        )
        self.store.replace_utterances(project.project_id, utterances)
        return RunContext(
            project=project, store=self.store, profile=self.profile, producer=MockProducer(),
            workspace=self.workspace, media=fixtures.media(),
            signals=SignalBundle(tension=fixtures.tension(), motion=[], silences=[]),
        )

    def test_a_twelve_hour_source_still_produces_one_event_per_thread(self):
        """An event mentioned in hour 1 and paid off in hour 9 must stay one event."""
        duration = 12 * 3600.0
        utterances = []
        for hour in range(12):
            base = hour * 3600.0
            utterances.append(Utterance(base + 10, base + 14, "the boss fight is still going", speaker="HOST"))
            utterances.append(Utterance(base + 100, base + 104, "unrelated chatter about lunch", speaker="HOST"))
        ctx = self._context(duration, utterances)

        understanding.run(ctx)
        events = self.store.events(ctx.project.project_id)
        self.assertTrue(events)
        widest = max(end - start for start, end in (e.span() for e in events))
        self.assertGreater(
            widest, 8 * 3600,
            "chunking lost the link between the early mention and the late payoff",
        )

    def test_chunking_does_not_drop_the_tail_of_the_broadcast(self):
        duration = 12 * 3600.0
        utterances = [
            Utterance(60, 64, "opening remarks about the tournament", speaker="HOST"),
            Utterance(duration - 200, duration - 196, "closing remarks about the tournament", speaker="HOST"),
        ]
        ctx = self._context(duration, utterances)
        understanding.run(ctx)

        windows = self.store.windows(ctx.project.project_id)
        self.assertAlmostEqual(windows[-1].end_sec, duration)
        latest = max(e.span()[1] for e in self.store.events(ctx.project.project_id))
        self.assertGreater(latest, duration - 3600)

    def test_a_short_source_takes_the_unchunked_path(self):
        ctx = self._context(fixtures.DURATION, fixtures.utterances())
        understanding.run(ctx)
        self.assertTrue(self.store.events(ctx.project.project_id))

    def test_merge_keeps_every_event_even_if_the_merge_pass_forgets_one(self):
        from aicut.models import Event, EventMention

        class Forgetful(MockProducer):
            def _task_merge_events(self, payload):
                return [{"member_indices": [0], "summary": "only the first", "people": []}]

        ctx = self._context(100.0, [])
        ctx.producer = Forgetful()
        partial = []
        for i in range(3):
            event = Event(project_id=ctx.project.project_id, summary=f"event {i}")
            event.mentions = [EventMention(event.event_id, i * 10, i * 10 + 5, "result", "")]
            partial.append(event)

        merged = understanding._merge_events(ctx, partial)
        self.assertEqual(len(merged), 3, "a forgotten event must survive as itself, not vanish")


class FaceSignalTests(unittest.TestCase):
    """5.3: without a face signal the talk/gameplay label is left UNKNOWN."""

    def setUp(self):
        self.profile = CalibrationProfile.load()

    def test_without_faces_the_label_is_unknown_not_guessed(self):
        from aicut.analysis.signals import label_situations

        spans = label_situations(200.0, [Utterance(0, 100, "talking", speaker="HOST")], [], self.profile)
        self.assertTrue(any(s.label is SituationLabel.UNKNOWN for s in spans))

    def test_a_big_face_reads_as_solo_talk_and_a_small_one_as_gameplay(self):
        from aicut.analysis.signals import label_situations

        speech = [Utterance(0, 40, "talking", speaker="HOST"), Utterance(60, 100, "still talking", speaker="HOST")]
        talk = label_situations(90.0, speech, [], self.profile, face_ratio=lambda a, b: 0.4)
        game = label_situations(90.0, speech, [], self.profile, face_ratio=lambda a, b: 0.01)
        self.assertTrue(any(s.label is SituationLabel.SOLO_TALK for s in talk))
        self.assertTrue(any(s.label is SituationLabel.GAMEPLAY for s in game))
        self.assertFalse(any(s.label is SituationLabel.SOLO_TALK for s in game))

    def test_face_ratio_lookup_averages_within_the_span(self):
        lookup = face_ratio_lookup([FaceReading(0, 0.2), FaceReading(5, 0.4), FaceReading(90, 0.9)])
        self.assertAlmostEqual(lookup(0, 10), 0.3)
        self.assertEqual(lookup(200, 210), 0.0)

    def test_expression_change_needs_two_readings(self):
        self.assertEqual(expression_change([FaceReading(0, 0.3, 1, (0, 0, 10, 10))], 0.0), 0.0)
        moved = expression_change(
            [FaceReading(0, 0.2, 1, (100, 100, 200, 200)), FaceReading(1, 0.5, 1, (600, 400, 400, 400))],
            0.5,
        )
        self.assertGreater(moved, 0.0)

    def test_missing_opencv_yields_no_detector_rather_than_an_error(self):
        from aicut.media.faces import available, build_detector

        detector = build_detector()
        self.assertEqual(detector is None, not available())


if __name__ == "__main__":
    unittest.main()
