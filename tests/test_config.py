import json
import tempfile
import unittest
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.errors import ConfigError, UnmeasuredParameterError


class ProfileTests(unittest.TestCase):
    def test_missing_parameter_is_an_error_not_a_default(self):
        profile = CalibrationProfile.load()
        with self.assertRaises(ConfigError):
            profile.get("pacing.no_such_knob")

    def test_reading_a_provisional_parameter_is_recorded(self):
        profile = CalibrationProfile.load()
        profile.get_float("silence.level_db")
        self.assertIn("silence.level_db", profile.touched_provisional())

    def test_strict_mode_refuses_unmeasured_values(self):
        profile = CalibrationProfile.load(strict=True)
        with self.assertRaises(UnmeasuredParameterError):
            profile.get_float("pacing.keep_score_threshold")
        # a documented standard is not a guess, so it still reads fine
        self.assertEqual(profile.get_float("render.audio.loudness.integrated_lufs"), -14.0)

    def test_channel_profile_layers_over_the_default(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "channel.json"
            path.write_text(json.dumps({
                "_meta": {"name": "ch", "measured": ["silence.level_db"]},
                "silence": {"level_db": -41.5},
            }), encoding="utf-8")
            profile = CalibrationProfile.load(path)
            self.assertEqual(profile.get_float("silence.level_db"), -41.5)
            self.assertFalse(profile.is_provisional("silence.level_db"))
            # untouched siblings keep both the default value and the guess mark
            self.assertEqual(profile.get_float("silence.min_duration_sec"), 0.35)
            self.assertTrue(profile.is_provisional("silence.min_duration_sec"))


if __name__ == "__main__":
    unittest.main()


class MeasurementVisibilityTests(unittest.TestCase):
    """Measuring something must be visible, or the step looks inert.

    `provisional` holds groups and `measured` holds leaves, so measuring
    `silence.level_db` correctly leaves the `silence` group flagged - two of its
    three values are still guesses. But every surface reported only the group
    count, so running step 1 of 17.4 changed nothing on screen and looked like
    it had failed.
    """

    def _measured(self):
        return CalibrationProfile.load().with_overrides(
            {"silence.level_db": -34.2}, measured=["silence.level_db"]
        )

    def test_a_measured_leaf_stops_being_provisional(self):
        profile = self._measured()
        self.assertFalse(profile.is_provisional("silence.level_db"))
        self.assertTrue(profile.is_provisional("silence.merge_gap_sec"),
                        "the rest of the group is still a guess and must stay flagged")

    def test_the_measurement_is_reported(self):
        self.assertEqual(self._measured().measured_parameters(), ["silence.level_db"])
        self.assertEqual(CalibrationProfile.load().measured_parameters(), [])

    def test_a_run_stops_naming_it_among_the_guesses_it_relied_on(self):
        """17.5: the report lists the unmeasured values a run actually read."""
        profile = self._measured()
        profile.get_float("silence.level_db")
        profile.get_float("silence.merge_gap_sec")
        self.assertEqual(profile.touched_provisional(), ["silence.merge_gap_sec"])

    def test_the_measurement_survives_a_save_and_reload(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "measured.json"
            self._measured().save(path)
            reloaded = CalibrationProfile.load(path)
            self.assertEqual(reloaded.measured_parameters(), ["silence.level_db"])
            self.assertFalse(reloaded.is_provisional("silence.level_db"))
            self.assertTrue(reloaded.is_provisional("silence.merge_gap_sec"))
