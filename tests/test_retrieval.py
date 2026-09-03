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


class SceneLengthRelativeToSourceTests(unittest.TestCase):
    """A scene may not be the broadcast, whatever the broadcast's length.

    The seconds cap alone missed this: on a real 30 second film the 90 second
    cap allowed a single scene covering all of it, and the plan came out as six
    cuts of 0.00-30.00 - every beat retrieving the entire source. Same failure
    as the six-hour case, too small for an absolute number to catch.
    """

    def _continuous(self, duration, step=1.2, spoken=1.0):
        out, t = [], 0.0
        while t + spoken < duration:
            out.append(Utterance(start_sec=t, end_sec=t + spoken,
                                 text="the bunny again", speaker="HOST"))
            t += step
        return out

    def test_a_thirty_second_source_is_not_one_scene(self):
        profile = CalibrationProfile.load()
        utterances = self._continuous(30.0)
        index = SceneIndex.build(utterances, [], profile=profile, source_sec=30.0)
        longest = max(s.end_sec - s.start_sec for s in index.scenes)
        self.assertLess(longest, 30.0, "one scene covered the whole source")
        self.assertGreater(len(index.scenes), 2,
                           "retrieval needs something to choose between")

    def test_the_ratio_is_a_profile_value(self):
        """17.1: not a constant in the code."""
        utterances = self._continuous(30.0)
        tight = CalibrationProfile.load().with_overrides(
            {"retrieval.scene_max_source_ratio": 0.1}, measured=[])
        loose = CalibrationProfile.load().with_overrides(
            {"retrieval.scene_max_source_ratio": 0.5}, measured=[])
        self.assertGreater(
            len(SceneIndex.build(utterances, [], profile=tight, source_sec=30.0).scenes),
            len(SceneIndex.build(utterances, [], profile=loose, source_sec=30.0).scenes),
        )

    def test_the_seconds_cap_still_binds_on_a_long_source(self):
        """Both bounds apply; on six hours the ratio is the loose one."""
        profile = CalibrationProfile.load()
        cap = profile.get_float("retrieval.scene_max_sec")
        utterances = self._continuous(2000.0, step=4.3, spoken=3.5)
        index = SceneIndex.build(utterances, [], profile=profile, source_sec=21600.0)
        self.assertLessEqual(max(s.end_sec - s.start_sec for s in index.scenes), cap + 10)


class MentionSceneCapTests(unittest.TestCase):
    """The cap has to hold on every path that makes a scene.

    Scenes built from speech were capped; scenes built from a mention with no
    speech under it were not. On a real film that branch put a 15.9 second
    scene into the plan while the cap said 6, so the cut it produced covered
    half the source - every cap in place and the failure still shipped.
    """

    def test_a_mention_with_no_speech_under_it_is_split_to_the_cap(self):
        profile = CalibrationProfile.load()
        event = Event(summary="a long silent stretch")
        event.mentions = [EventMention(event.event_id, 0.0, 60.0, "result", "no words here")]

        index = SceneIndex.build([], [event], profile=profile, source_sec=300.0)
        cap = min(profile.get_float("retrieval.scene_max_sec"),
                  300.0 * profile.get_float("retrieval.scene_max_source_ratio"))
        self.assertTrue(index.scenes, "the mention became nothing at all")
        longest = max(s.end_sec - s.start_sec for s in index.scenes)
        self.assertLessEqual(longest, cap + 0.01,
                             f"a mention-built scene ran {longest:.1f}s past a {cap:.1f}s cap")

    def test_the_whole_mention_is_still_covered(self):
        """Split, not truncated: 16장 keeps working through silence."""
        event = Event(summary="silence")
        event.mentions = [EventMention(event.event_id, 10.0, 70.0, "result", "quiet")]
        index = SceneIndex.build([], [event], profile=CalibrationProfile.load(), source_sec=300.0)
        spans = sorted((s.start_sec, s.end_sec) for s in index.scenes)
        self.assertAlmostEqual(spans[0][0], 10.0)
        self.assertAlmostEqual(spans[-1][1], 70.0)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertAlmostEqual(end, start, msg="a gap opened between the pieces")


class LongUtteranceSplitTests(unittest.TestCase):
    """The cap has to hold inside an utterance too.

    Scenes split between utterances, so an utterance longer than the cap went
    through whole. A recogniser running over music merges its guesses into one
    long span, and on a real 30 second film that produced a single 14.9s
    utterance against a 6s cap - the third place this same failure hid, after
    no cap at all and an absolute-only cap.
    """

    def _profile(self, cap):
        return CalibrationProfile.load().with_overrides(
            {"retrieval.scene_max_sec": cap,
             "retrieval.scene_max_source_ratio": 1.0}, measured=[])

    def test_one_long_utterance_becomes_several_scenes(self):
        words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(40)]
        utterance = Utterance(start_sec=0.0, end_sec=20.0, text=" ".join(w["word"] for w in words),
                              speaker="HOST", words=words)
        index = SceneIndex.build([utterance], [], profile=self._profile(6.0), source_sec=300.0)
        self.assertGreater(len(index.scenes), 1, "a 20s utterance stayed one scene under a 6s cap")
        self.assertLessEqual(max(s.end_sec - s.start_sec for s in index.scenes), 6.5)

    def test_the_pieces_carry_the_words_that_were_said_in_them(self):
        words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(40)]
        utterance = Utterance(start_sec=0.0, end_sec=20.0, text=" ".join(w["word"] for w in words),
                              speaker="HOST", words=words)
        scenes = sorted(SceneIndex.build([utterance], [], profile=self._profile(6.0),
                                         source_sec=300.0).scenes, key=lambda s: s.start_sec)
        self.assertIn("w0", scenes[0].text)
        self.assertNotIn("w0", scenes[-1].text, "a later piece claimed words from the start")
        self.assertTrue(scenes[-1].text.strip(), "the last piece lost its text")

    def test_an_utterance_with_no_word_timings_is_still_split(self):
        """No seams to cut on, so the text stays with the first piece rather
        than being invented into parts nobody said."""
        utterance = Utterance(start_sec=0.0, end_sec=20.0, text="one long stretch", speaker="HOST")
        scenes = SceneIndex.build([utterance], [], profile=self._profile(6.0), source_sec=300.0).scenes
        self.assertGreater(len(scenes), 1)
        self.assertLessEqual(max(s.end_sec - s.start_sec for s in scenes), 6.5)
