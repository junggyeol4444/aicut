"""EDL / FCPXML / SRT export (22.5).

An exchange file that an editor half-reads is worse than none: the timeline
looks plausible and the cuts are in the wrong places. These check the numbers
an importer actually reads, and parse the XML back rather than trusting that
the string was assembled correctly.
"""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction

from aicut.errors import AicutError
from aicut.models import Cut, Episode, SubtitleLine
from aicut.render.editplan import EditPlan
from aicut.render.exchange import (
    UnsupportedFrameRate,
    media_src,
    timecode,
    to_edl,
    to_fcpxml,
    to_srt,
)


def _plan(cuts, subtitles=None, *, target="long", settings=None):
    episode = Episode(project_id="p", target_type=target, timeline=cuts)
    episode.subtitles = subtitles or []
    return EditPlan.from_episode(
        episode, "/broadcasts/stream one.mkv",
        render_settings=settings or {"fps": 30, "width": 1920, "height": 1080},
    )


class TimecodeTests(unittest.TestCase):
    def test_whole_seconds(self):
        self.assertEqual(timecode(0.0, 30), "00:00:00:00")
        self.assertEqual(timecode(1.0, 30), "00:00:01:00")
        self.assertEqual(timecode(3661.0, 30), "01:01:01:00")

    def test_frames_are_the_remainder_not_a_decimal(self):
        self.assertEqual(timecode(1.5, 30), "00:00:01:15")
        # 1.5s at 25fps is frame 37.5; Python rounds half to even, so 38,
        # which is 1 second and 13 frames.
        self.assertEqual(timecode(1.5, 25), "00:00:01:13")

    def test_ntsc_rates_count_at_the_whole_rate(self):
        """29.97 non-drop-frame counts 30 frames per timecode second - that is
        what NDF means, and why the file says so in its FCM line."""
        self.assertEqual(timecode(1.0, 29.97), "00:00:01:00")
        self.assertEqual(timecode(1.0, 23.976), "00:00:01:00")

    def test_a_rate_with_no_honest_timecode_is_refused(self):
        with self.assertRaises(UnsupportedFrameRate):
            timecode(1.0, 12.5)

    def test_negative_time_is_a_bug_not_a_wrap(self):
        with self.assertRaises(ValueError):
            timecode(-1.0, 30)


class EdlTests(unittest.TestCase):
    def setUp(self):
        self.plan = _plan([
            Cut(0, 10.0, 12.0),
            Cut(1, 100.0, 103.0),
        ])
        self.text = to_edl(self.plan, 30)

    def test_it_is_non_drop_frame_and_says_so(self):
        self.assertIn("FCM: NON-DROP FRAME", self.text)

    def test_record_times_are_contiguous_from_zero(self):
        """The output timeline has no gaps: cut two starts where cut one ended."""
        rows = [l for l in self.text.splitlines() if l[:3].isdigit()]
        self.assertEqual(len(rows), 2)
        self.assertIn("00:00:00:00 00:00:02:00", rows[0])   # record in/out, 2s
        self.assertIn("00:00:02:00 00:00:05:00", rows[1])   # continues, 3s

    def test_source_times_are_where_the_cut_came_from(self):
        rows = [l for l in self.text.splitlines() if l[:3].isdigit()]
        self.assertIn("00:00:10:00 00:00:12:00", rows[0])
        self.assertIn("00:01:40:00 00:01:43:00", rows[1])

    def test_the_reel_name_survives_a_path_an_editor_would_choke_on(self):
        """Reel names are 8 characters of A-Z0-9 in practice."""
        plan = _plan([Cut(0, 0.0, 1.0)])
        plan.source_path = "/방송/한글 이름 (2026).mkv"
        rows = [l for l in to_edl(plan, 30).splitlines() if l[:3].isdigit()]
        # The field is a fixed 8 columns after the event number and two spaces;
        # split() would eat the padding that makes it fixed.
        field = rows[0][5:13]
        self.assertEqual(len(field), 8)
        reel = field.strip()
        self.assertTrue(reel, "the reel name vanished entirely")
        self.assertTrue(reel.isalnum(), f"reel {reel!r} is not alphanumeric")
        self.assertTrue(reel.isascii(), f"reel {reel!r} is not ASCII")

    def test_a_plan_with_nothing_in_it_is_refused(self):
        with self.assertRaises(AicutError):
            to_edl(_plan([]), 30)


class FcpxmlTests(unittest.TestCase):
    def setUp(self):
        self.plan = _plan(
            [Cut(0, 100.0, 103.0), Cut(1, 10.0, 12.0)],
            settings={"fps": 30, "width": 1080, "height": 1920},
        )
        self.root = ET.fromstring(to_fcpxml(self.plan, 30, source_size=(1920, 1080),
                                            source_duration_sec=600.0))

    def _seconds(self, value: str) -> float:
        return float(Fraction(value.rstrip("s")))

    def test_it_parses(self):
        self.assertEqual(self.root.tag, "fcpxml")

    def test_the_source_keeps_its_own_shape_not_the_sequence_s(self):
        """A 1920x1080 clip pointed at a vertical sequence format gets
        letterboxed by the importer, which looks like the plan asked for it."""
        formats = {f.get("id"): f for f in self.root.iter("format")}
        asset = next(self.root.iter("asset"))
        self.assertEqual(formats[asset.get("format")].get("width"), "1920")
        self.assertEqual(formats[asset.get("format")].get("height"), "1080")
        sequence = next(self.root.iter("sequence"))
        self.assertEqual(formats[sequence.get("format")].get("height"), "1920")

    def test_clips_are_laid_end_to_end_in_plan_order(self):
        clips = list(self.root.iter("asset-clip"))
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(self._seconds(clips[0].get("offset")), 0.0)
        self.assertAlmostEqual(self._seconds(clips[0].get("duration")), 3.0)
        self.assertAlmostEqual(self._seconds(clips[1].get("offset")), 3.0)
        self.assertAlmostEqual(self._seconds(clips[1].get("duration")), 2.0)

    def test_the_first_clip_is_the_later_source_moment(self):
        """2.4: output order is not source order, and the export must not
        quietly sort it back."""
        clips = list(self.root.iter("asset-clip"))
        self.assertAlmostEqual(self._seconds(clips[0].get("start")), 100.0)
        self.assertAlmostEqual(self._seconds(clips[1].get("start")), 10.0)

    def test_the_source_is_referenced_by_path(self):
        rep = next(self.root.iter("media-rep"))
        self.assertIn("stream%20one.mkv", rep.get("src"))


class MediaSrcTests(unittest.TestCase):
    """The `src` must be a URL on whatever machine the export runs on.

    This was written with `Path.as_uri()`, whose idea of absolute is the
    running platform's. A plan written on Linux carries `/broadcasts/x.mkv`,
    Windows reads that as relative, and the export emitted a bare path with raw
    spaces in it - which an importer either fails to relink or truncates at the
    space. Windows CI caught it; these tests are what keeps it caught.
    """

    def test_a_posix_path_is_a_file_url_with_spaces_encoded(self):
        self.assertEqual(media_src("/broadcasts/stream one.mkv"),
                         "file:///broadcasts/stream%20one.mkv")

    def test_a_windows_path_keeps_its_drive_and_turns_slashes_round(self):
        self.assertEqual(media_src(r"C:\Users\j\stream one.mkv"),
                         "file:///C:/Users/j/stream%20one.mkv")

    def test_a_relative_path_stays_relative_but_is_still_encoded(self):
        self.assertEqual(media_src("clips/stream one.mkv"), "clips/stream%20one.mkv")

    def test_a_url_is_left_alone(self):
        self.assertEqual(media_src("file:///already/encoded%20name.mkv"),
                         "file:///already/encoded%20name.mkv")

    def test_a_windows_path_read_on_linux_still_names_the_clip(self):
        """`Path(...).name` reads the separators of the machine it runs on, so
        a Windows path opened on Linux kept its whole drive prefix and that
        went into the timeline as the clip label."""
        from aicut.render.exchange import source_name

        self.assertEqual(source_name(r"C:\Users\j\stream.mkv"), "stream.mkv")
        self.assertEqual(source_name("/broadcasts/stream.mkv"), "stream.mkv")
        self.assertEqual(source_name("stream.mkv"), "stream.mkv")

    def test_a_korean_name_is_encoded_not_dropped(self):
        src = media_src("/방송/한글 이름 (2026).mkv")
        self.assertTrue(src.startswith("file:///"))
        self.assertNotIn(" ", src)
        self.assertTrue(src.isascii(), "an XML attribute an importer must parse")

    def test_ntsc_times_are_rational_not_decimal(self):
        root = ET.fromstring(to_fcpxml(_plan([Cut(0, 0.0, 1.001)],
                                             settings={"fps": 29.97}), 29.97))
        clip = next(root.iter("asset-clip"))
        self.assertIn("/30000s", clip.get("duration"),
                      "29.97 must be written on a 1001/30000 grid, not as seconds")


class SrtTests(unittest.TestCase):
    def test_times_are_on_the_output_clock(self):
        plan = _plan([Cut(0, 500.0, 505.0)], [SubtitleLine(0.5, 2.25, "he wins")])
        text = to_srt(plan)
        self.assertIn("00:00:00,500 --> 00:00:02,250", text)
        self.assertIn("he wins", text)

    def test_lines_are_numbered_in_order(self):
        plan = _plan([Cut(0, 0.0, 10.0)], [
            SubtitleLine(4.0, 5.0, "second"),
            SubtitleLine(1.0, 2.0, "first"),
        ])
        blocks = to_srt(plan).strip().split("\n\n")
        self.assertTrue(blocks[0].startswith("1\n"))
        self.assertIn("first", blocks[0])
        self.assertIn("second", blocks[1])


if __name__ == "__main__":
    unittest.main()
