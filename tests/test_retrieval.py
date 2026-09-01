import unittest

from aicut.config import CalibrationProfile
from aicut.models import Event, EventMention, Utterance
from aicut.pipeline.retrieval import SceneIndex
from tests import fixtures


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.profile = CalibrationProfile.load()
        self.utterances = fixtures.utterances()
        event = Event(summary="the boss fight")
        event.mentions = [
            EventMention(event.event_id, 30, 34, "first_mention", "this boss has killed me"),
            EventMention(event.event_id, 1830, 1835, "result", "i beat the boss"),
        ]
        self.event = event
        self.index = SceneIndex.build(self.utterances, [event])

    def test_search_reaches_across_the_whole_broadcast(self):
        """8.1: the shot a beat needs may sit hours away (2.4)."""
        results = self.index.search("boss", self.profile)
        times = [r.scene.start_sec for r in results]
        self.assertTrue(any(t < 200 for t in times))
        self.assertTrue(any(t > 1500 for t in times))

    def test_event_membership_outranks_wording(self):
        results = self.index.search("beat", self.profile, event_id=self.event.event_id, role="result")
        self.assertIn("result", results[0].scene.roles)

    def test_locality_is_a_preference_not_a_filter(self):
        near = self.index.search("boss", self.profile, near_sec=1830.0)
        self.assertTrue(any(r.scene.start_sec < 200 for r in near),
                        "a distant scene must still be able to compete")

    def test_scenes_split_on_speaker_change(self):
        speakers = {s.speaker for s in self.index.scenes}
        self.assertIn("GUEST", speakers)
        self.assertIn("HOST", speakers)

    def test_a_mention_with_no_speech_under_it_is_still_retrievable(self):
        """16장: silence must not make a moment invisible to the planner."""
        event = Event(summary="the silent stare")
        event.mentions = [EventMention(event.event_id, 2600, 2640, "reaction", "no words, just a look")]
        index = SceneIndex.build([], [event])
        results = index.search("look", self.profile, event_id=event.event_id)
        self.assertTrue(results)
        self.assertEqual(results[0].scene.start_sec, 2600)

    def test_below_threshold_matches_are_dropped(self):
        self.assertEqual(self.index.search("quantum tunnelling diode", self.profile), [])


class DiscoveryRuleTests(unittest.TestCase):
    """6.2: the unit of division is the event, not the screen."""

    def test_candidates_without_an_event_anchor_are_dropped(self):
        import tempfile
        from pathlib import Path

        from aicut.db.store import Store
        from aicut.llm.mock import MockProducer
        from aicut.models import Project
        from aicut.pipeline import discovery
        from aicut.pipeline.context import RunContext

        class Unanchored(MockProducer):
            def _task_discover_candidates(self, payload):
                return [{
                    "core_summary": "a vibe, from nowhere in particular",
                    "related_event_ids": ["not-an-event"],
                    "independence_score": 0.9, "density_score": 0.9, "has_resolution": True,
                }]

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            project = store.create_project(Project(file_path="/f.mkv", duration_sec=100))
            event = Event(project_id=project.project_id, summary="something real")
            event.mentions = [EventMention(event.event_id, 10, 20, "result", "there")]
            store.replace_events(project.project_id, [event])
            ctx = RunContext(
                project=project, store=store, profile=CalibrationProfile.load(),
                producer=Unanchored(), workspace=Path(tmp),
            )
            self.assertEqual(discovery.run(ctx), [])
            store.close()


class CombineTests(unittest.TestCase):
    """6.3 candidate B: funny but unresolved, only shippable once welded to its ending."""

    def test_combine_links_are_merged_into_one_episode_group(self):
        from aicut.models import ContentCandidate, Decision
        from aicut.pipeline.evaluating import group_for_production

        a = ContentCandidate(core_summary="the setup", decision=Decision.COMBINE, has_resolution=False)
        b = ContentCandidate(core_summary="the payoff", decision=Decision.PRODUCE)
        a.combine_with = [b.candidate_id]
        groups = group_for_production([a, b])
        self.assertEqual(len(groups), 1)
        self.assertEqual({c.candidate_id for c in groups[0]}, {a.candidate_id, b.candidate_id})

    def test_an_unresolved_candidate_alone_is_not_produced(self):
        from aicut.models import ContentCandidate, Decision
        from aicut.pipeline.evaluating import group_for_production

        orphan = ContentCandidate(core_summary="no ending", decision=Decision.COMBINE, has_resolution=False)
        self.assertEqual(group_for_production([orphan]), [])


if __name__ == "__main__":
    unittest.main()


class SceneLengthTests(unittest.TestCase):
    """A scene is the unit retrieval picks from, so it cannot be the whole source.

    Measured on a real six-hour run: a host talking with sub-second gaps
    produced ONE scene spanning the entire broadcast, because scenes were only
    ever split on a pause or a speaker change. Every beat then retrieved that
    one scene, and the resulting plan had 179 cuts covering 3,830,063 seconds
    of a 21,600 second source, with 828,949 subtitle lines.
    """

    SOURCE_SEC = 21600

    def _continuous_talk(self, step=4.3, spoken=3.5):
        """Six hours of speech whose gaps never reach the pause threshold."""
        out, t = [], 5.0
        while t < self.SOURCE_SEC - 200:
            out.append(Utterance(
                start_sec=t, end_sec=t + spoken, text="the tournament bracket again",
                speaker="HOST",
            ))
            t += step
        return out

    def test_no_scene_swallows_the_broadcast(self):
        profile = CalibrationProfile.load()
        cap = profile.get_float("retrieval.scene_max_sec")
        utterances = self._continuous_talk()
        self.assertGreater(len(utterances), 4000, "the fixture must be six hours of talk")

        index = SceneIndex.build(utterances, [], profile=profile)
        longest = max(s.end_sec - s.start_sec for s in index.scenes)
        self.assertLessEqual(
            longest, cap + 10,
            f"one scene ran {longest:.0f}s; retrieval cannot pick a moment out of the whole broadcast",
        )
        self.assertGreater(len(index.scenes), 100, "six hours collapsed into a handful of scenes")

    def test_the_cap_is_a_profile_parameter_not_a_constant(self):
        """17.1: every judgement threshold lives in the profile."""
        utterances = self._continuous_talk()
        short = CalibrationProfile.load().with_overrides(
            {"retrieval.scene_max_sec": 20.0}, measured=[])
        long = CalibrationProfile.load().with_overrides(
            {"retrieval.scene_max_sec": 300.0}, measured=[])
        self.assertGreater(
            len(SceneIndex.build(utterances, [], profile=short).scenes),
            len(SceneIndex.build(utterances, [], profile=long).scenes),
            "the cap had no effect, so it is not the thing deciding scene length",
        )

    def test_a_real_pause_still_ends_a_scene_before_the_cap(self):
        profile = CalibrationProfile.load()
        gap = profile.get_float("retrieval.scene_gap_sec")
        utterances = [
            Utterance(start_sec=0.0, end_sec=3.0, text="before the pause", speaker="HOST"),
            Utterance(start_sec=3.0 + gap + 1, end_sec=9.0 + gap, text="after the pause", speaker="HOST"),
        ]
        index = SceneIndex.build(utterances, [], profile=profile)
        self.assertEqual(len(index.scenes), 2, "the pause no longer splits a scene")
