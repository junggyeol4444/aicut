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
