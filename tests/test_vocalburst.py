"""Laughter and scream detection (9.1) and its effect on tension."""

import unittest

from aicut.analysis.tension import build_tension_curve
from aicut.analysis.vocalburst import LoudNonSpeechDetector, build_detector
from aicut.config import CalibrationProfile
from aicut.models import Utterance


def quiet_then(loud_spans, duration=120):
    """RMS envelope: quiet baseline with loud stretches at the given spans."""
    rms = []
    for i in range(duration):
        level = -32.0
        for start, end in loud_spans:
            if start <= i < end:
                level = -8.0
        rms.append((float(i), level))
    return rms


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.profile = CalibrationProfile.load()
        self.detector = LoudNonSpeechDetector()

    def test_a_wordless_loud_stretch_is_a_burst(self):
        bursts = self.detector.detect(quiet_then([(30, 34)]), [], self.profile)
        self.assertEqual(len(bursts), 1)
        self.assertEqual((bursts[0].start_sec, bursts[0].end_sec), (30.0, 34.0))
        self.assertEqual(bursts[0].word_density, 0.0)

    def test_loud_talking_is_not_a_burst(self):
        """Excited speech is loud too; the words are what separate them."""
        speech = [Utterance(30, 34, "oh my god look at that right now no way absolutely insane")]
        self.assertEqual(self.detector.detect(quiet_then([(30, 34)]), speech, self.profile), [])

    def test_a_burst_shorter_than_the_profile_minimum_is_ignored(self):
        rms = quiet_then([(30, 31)])
        strict = self.profile.with_overrides({"laughter.min_duration_sec": 3.0}, measured=[])
        self.assertEqual(strict.get_float("laughter.min_duration_sec"), 3.0)
        self.assertEqual(self.detector.detect(rms, [], strict), [])
        self.assertTrue(self.detector.detect(rms, [], self.profile.with_overrides(
            {"laughter.min_duration_sec": 0.5}, measured=[])))

    def test_nearby_bursts_merge_across_a_breath(self):
        bursts = self.detector.detect(quiet_then([(30, 33), (34, 37)]), [], self.profile)
        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0].end_sec, 37.0)

    def test_intensity_is_relative_to_this_broadcast(self):
        hot = self.detector.detect(quiet_then([(30, 34)]), [], self.profile)
        # the same shape recorded 20 dB quieter must score the same
        quiet_rms = [(t, level - 20.0) for t, level in quiet_then([(30, 34)])]
        cold = self.detector.detect(quiet_rms, [], self.profile)
        self.assertEqual(len(hot), len(cold))
        self.assertAlmostEqual(hot[0].intensity, cold[0].intensity, places=4)

    def test_no_audio_yields_no_bursts(self):
        self.assertEqual(self.detector.detect([], [], self.profile), [])

    def test_detector_can_be_switched_off_by_profile(self):
        self.assertIsNone(build_detector("none"))
        self.assertIsInstance(build_detector("loud_non_speech"), LoudNonSpeechDetector)


class TensionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = CalibrationProfile.load()

    def test_a_burst_raises_tension_where_it_happens(self):
        rms = quiet_then([(30, 34)])
        detector = LoudNonSpeechDetector()
        signal = detector.as_signal(detector.detect(rms, [], self.profile))
        with_laughter = build_tension_curve(rms, [], self.profile, laughter=signal)
        without = build_tension_curve(rms, [], self.profile)
        self.assertGreater(with_laughter.peak(30, 34), without.peak(30, 34))

    def test_absent_detector_redistributes_the_weight_instead_of_scoring_zero(self):
        """9.1: a missing signal must not drag every moment down."""
        rms = quiet_then([(30, 34)])
        without = build_tension_curve(rms, [], self.profile)
        zeroed = build_tension_curve(rms, [], self.profile, laughter=[])
        self.assertGreater(without.peak(30, 34), zeroed.peak(30, 34))


if __name__ == "__main__":
    unittest.main()
