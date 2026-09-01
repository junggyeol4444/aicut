"""Renderer tests that actually run ffmpeg.

Everything else in the suite checks the commands the renderer builds. These
check that the commands do what the plan says when a real file goes through
them, which is the only way to catch the class of bug that lives between a
correct-looking argument list and ffmpeg's actual behaviour. Skipped when
ffmpeg is not installed, so the offline suite still runs anywhere.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.media.ffmpeg_util import (
    _parse_filter_list,
    available_filters,
    has_filter,
    have_ffmpeg,
)
from aicut.models import Cut, Episode, PacingMode, SubtitleLine
from aicut.render.editplan import EditPlan
from aicut.render.ffmpeg import Renderer
from aicut.render.subtitles import SubtitleStyleProfile, write_ass

SOURCE_SEC = 12


def _fast_profile() -> CalibrationProfile:
    """The shipped profile, encoding as fast as possible - quality is not the subject."""
    return CalibrationProfile.load().with_overrides(
        {"render.video.preset": "ultrafast", "render.video.crf": 35}, measured=[]
    )


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _psnr(a: Path, b: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b), "-filter_complex", "psnr", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    for token in out.stderr.split():
        if token.startswith("average:"):
            return float(token.split(":")[1])
    raise AssertionError("ffmpeg printed no PSNR")


def _frame(video: Path, at_sec: float, out: Path, crop: str = "scale=640:360") -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-ss", f"{at_sec}", "-i", str(video),
         "-frames:v", "1", "-vf", crop, "-y", str(out)],
        check=True,
    )
    return out


@unittest.skipUnless(have_ffmpeg(), "ffmpeg is not installed")
class LiveRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.dir = Path(cls._tmp.name)
        cls.source = cls.dir / "src.mkv"
        # A source whose picture changes every second, so a frame identifies its
        # own timestamp and a mis-seeked cut cannot pass unnoticed.
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=10:duration={SOURCE_SEC}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={SOURCE_SEC}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.source),
        ], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _render(self, episode: Episode, name: str, *, work_dir: Path | None = None, ass=None) -> Path:
        plan = EditPlan.from_episode(episode, str(self.source))
        out = self.dir / f"{name}.mp4"
        Renderer(_fast_profile(), work_dir or (self.dir / "work")).render(plan, out, ass_path=ass)
        return out

    def test_remove_spans_are_actually_dropped_from_the_file(self):
        """9.3 CUT/TRIM is a promise about the output, not an annotation."""
        episode = Episode(project_id="p", timeline=[
            Cut(0, 0.0, 8.0, pacing_mode=PacingMode.CUT, remove_spans=[[2.0, 5.0]]),
        ])
        out = self._render(episode, "removed")
        self.assertAlmostEqual(_probe_duration(out), 5.0, delta=0.25)

    def test_cuts_render_in_plan_order_not_source_order(self):
        """2.4: the finished video opens on the later moment because the plan says so."""
        episode = Episode(project_id="p", timeline=[
            Cut(0, 8.0, 10.0, scene_role="result"),
            Cut(1, 1.0, 3.0, scene_role="background"),
        ])
        out = self._render(episode, "reordered")
        self.assertAlmostEqual(_probe_duration(out), 4.0, delta=0.25)

        opening = _frame(out, 0.5, self.dir / "opening.png")
        late_source = _frame(self.source, 8.5, self.dir / "late.png")
        early_source = _frame(self.source, 1.5, self.dir / "early.png")
        self.assertGreater(
            _psnr(opening, late_source), _psnr(opening, early_source),
            "the output opens on the early source moment: plan order was not honoured",
        )

    def test_a_relative_work_directory_still_concatenates(self):
        """Regression: concat resolves list entries against the list file's own
        directory, so a relative segment path was joined onto the stage twice."""
        import os

        episode = Episode(project_id="p", timeline=[Cut(0, 0.0, 2.0), Cut(1, 4.0, 6.0)])
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            out = self._render(episode, "relwork", work_dir=Path("relative_work"))
        finally:
            os.chdir(cwd)
        self.assertAlmostEqual(_probe_duration(out), 4.0, delta=0.25)

    @unittest.skipUnless(has_filter("subtitles"), "this ffmpeg was built without libass")
    def test_subtitles_are_burned_in_where_the_plan_puts_them(self):
        episode = Episode(project_id="p", timeline=[Cut(0, 0.0, 6.0)])
        episode.subtitles = [SubtitleLine(0.5, 2.5, "BURNED IN", emphasis=True)]
        ass = write_ass(episode.subtitles, self.dir / "subs.ass", SubtitleStyleProfile.load("default"))
        out = self._render(episode, "subtitled", ass=ass)

        strip = "scale=1920:1080,crop=1920:216:0:820"
        during = _psnr(
            _frame(out, 1.5, self.dir / "sub_out.png", strip),
            _frame(self.source, 1.5, self.dir / "sub_src.png", strip),
        )
        after = _psnr(
            _frame(out, 4.5, self.dir / "nosub_out.png", strip),
            _frame(self.source, 4.5, self.dir / "nosub_src.png", strip),
        )
        self.assertLess(during, after - 5, "no overlay appeared where the subtitle should be")

    def test_a_failed_render_leaves_its_segments_and_says_where(self):
        """16장: a failed render must not cost the plan. It should not cost the
        disk silently either - the segments stay for inspection, but the log
        names the directory holding them."""
        from aicut.errors import RenderError

        episode = Episode(project_id="p", timeline=[Cut(0, 0.0, 4.0)])
        plan = EditPlan.from_episode(episode, str(self.source))
        work = self.dir / "failwork"
        renderer = Renderer(_fast_profile(), work)

        # An .ass path that is not a subtitle file: the segments cut fine and
        # the final command is what fails, which is the case that leaks.
        broken = self.dir / "not_subtitles.ass"
        broken.write_text("this is not an ass file at all", encoding="utf-8")

        with self.assertLogs("aicut.render.ffmpeg", level="WARNING") as logged:
            with self.assertRaises(RenderError):
                renderer.render(plan, self.dir / "never.mp4", ass_path=broken)

        stage = work / plan.episode_id
        self.assertTrue(stage.exists(), "the segments were deleted, so there is nothing to inspect")
        self.assertTrue(any(stage.glob("seg_*.mp4")), "no segments survived the failure")
        self.assertTrue(any(str(stage) in line for line in logged.output),
                        "the log does not say where the leftover bytes are")

    def test_two_pass_loudness_hits_the_target(self):
        """10.4-3: measure then apply, so a stitched timeline holds one level."""
        episode = Episode(project_id="p", timeline=[Cut(0, 0.0, 4.0), Cut(1, 6.0, 10.0)])
        out = self._render(episode, "loudness")
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(out),
             "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        )
        line = next(l for l in result.stderr.splitlines() if l.strip().startswith("I:"))
        measured = float(line.split()[1])
        target = CalibrationProfile.load().get_float("render.audio.loudness.integrated_lufs")
        self.assertAlmostEqual(measured, target, delta=1.0)

    def test_a_shorts_episode_renders_vertical(self):
        episode = Episode(project_id="p", target_type="shorts", timeline=[Cut(0, 0.0, 3.0)])
        out = self._render(episode, "vertical")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(probe.stdout.strip(), "1080,1920")

    def test_zoom_actually_crops_the_frame(self):
        """10.4-1: the replacement expression has to move pixels, not just parse."""
        plain = Episode(project_id="p", timeline=[Cut(0, 2.0, 4.0)])
        zoomed = Episode(project_id="p", timeline=[
            Cut(0, 2.0, 4.0, visual_effect={"type": "zoom", "scale": 0.5, "center": [0.3, 0.3]}),
        ])
        a = self._render(plain, "plain")
        b = self._render(zoomed, "zoomed")
        self.assertLess(
            _psnr(_frame(b, 1.0, self.dir / "z.png"), _frame(a, 1.0, self.dir / "p.png")),
            30.0,
            "the zoomed render is indistinguishable from the unzoomed one",
        )

    def test_sendcmd_zoom_pans_the_frame_over_time(self):
        """10.4-1 strategy (b). It pans at a fixed crop size; see sendcmd_file."""
        keyframes = [
            {"at_sec": 0, "scale": 0.6, "center": [0.15, 0.15]},
            {"at_sec": 3, "scale": 0.6, "center": [0.85, 0.85]},
        ]
        panning = Episode(project_id="p", timeline=[
            Cut(0, 2.0, 8.0, visual_effect={"type": "zoom", "scale": 0.6, "center": [0.15, 0.15],
                                            "keyframes": keyframes}),
        ])
        fixed = Episode(project_id="p", timeline=[
            Cut(0, 2.0, 8.0, visual_effect={"type": "zoom", "scale": 0.6, "center": [0.15, 0.15]}),
        ])
        moving_profile = _fast_profile().with_overrides({"render.zoom.strategy": "sendcmd"}, measured=[])
        plan = EditPlan.from_episode(panning, str(self.source))
        moved = self.dir / "panned.mp4"
        Renderer(moving_profile, self.dir / "work_pan").render(plan, moved)
        still = self._render(fixed, "fixed_crop")

        before = _psnr(_frame(moved, 0.5, self.dir / "pan0.png"), _frame(still, 0.5, self.dir / "fix0.png"))
        after = _psnr(_frame(moved, 5.0, self.dir / "pan5.png"), _frame(still, 5.0, self.dir / "fix5.png"))
        self.assertGreater(before, after, "the camera never moved away from its starting position")

    def test_sendcmd_with_mixed_scales_flattens_instead_of_stalling(self):
        """crop w/h commands hang the graph (measured), so scale keyframes are
        flattened to one crop size and the file must still come out."""
        from aicut.render.ffmpeg import sendcmd_file

        path = sendcmd_file(
            [{"at_sec": 0, "scale": 0.9, "center": [0.2, 0.2]},
             {"at_sec": 3, "scale": 0.45, "center": [0.8, 0.8]}],
            self.dir / "mixed.cmd",
        )
        text = path.read_text()
        self.assertNotIn("crop w", text)
        self.assertNotIn("crop h", text)
        self.assertEqual(text.count("crop x"), 2)

        episode = Episode(project_id="p", timeline=[
            Cut(0, 2.0, 6.0, visual_effect={"type": "zoom", "scale": 0.9, "center": [0.2, 0.2],
                                            "keyframes": [{"at_sec": 0, "scale": 0.9, "center": [0.2, 0.2]},
                                                          {"at_sec": 2, "scale": 0.45, "center": [0.8, 0.8]}]}),
        ])
        profile = _fast_profile().with_overrides({"render.zoom.strategy": "sendcmd"}, measured=[])
        out = self.dir / "mixed.mp4"
        Renderer(profile, self.dir / "work_mixed").render(EditPlan.from_episode(episode, str(self.source)), out)
        self.assertAlmostEqual(_probe_duration(out), 4.0, delta=0.25)

    def test_an_empty_timeline_is_refused_rather_than_producing_a_broken_file(self):
        from aicut.errors import RenderError

        with self.assertRaises(RenderError):
            self._render(Episode(project_id="p", timeline=[]), "empty")


class FilterListParseTests(unittest.TestCase):
    """The layout of `ffmpeg -filters` is not stable, and getting it wrong is
    not a near miss: an empty set makes `require_filter` read every build as
    incapable and refuse to render.

    ffmpeg 7 prints three flag characters (`T`/`S`/`C`), ffmpeg 8 prints two -
    the command-support flag was dropped, and a parser pinned to three matched
    nothing at all. Both layouts are pinned here, from ffmpeg's own format
    strings, so the next change to the table is a failing test rather than a
    build that claims it cannot crop.
    """

    SEVEN = (
        "Filters:\n"
        "  T.. = Timeline support\n"
        "  .S. = Slice threading\n"
        "  ..C = Command support\n"
        "  A = Audio input/output\n"
        "  | = Source or sink filter\n"
        "  ------\n"
        " TSC adenorm           A->A       Remedy denormals.\n"
        " ... acrossfade        AA->A      Cross fade two input audio streams.\n"
        " T.C crop              V->V       Crop the input video.\n"
        " ... subtitles         V->V       Render text subtitles.\n"
        " ..C amovie            |->N       Read audio from a movie source.\n"
    )

    # ffmpeg 8/9: " %c%c %-17s %-10s %s" - two flag characters in the rows.
    # Copied from a real `ffmpeg 9.0.1 -filters`, legend included: note that the
    # legend still writes three-character masks (`T..`) while the rows carry
    # two, so anything keyed on the legend's width parses the table wrongly.
    EIGHT = (
        "Filters:\n"
        "  T.. = Timeline support\n"
        "  .S. = Slice threading\n"
        "  A = Audio input/output\n"
        "  V = Video input/output\n"
        "  N = Dynamic number and/or type of input/output\n"
        "  | = Source or sink filter\n"
        "  ------\n"
        " TS adenorm           A->A       Remedy denormals.\n"
        " .. acrossfade        AA->A      Cross fade two input audio streams.\n"
        " T. crop              V->V       Crop the input video.\n"
        " .. subtitles         V->V       Render text subtitles.\n"
        " .. amovie            |->N       Read audio from a movie source.\n"
    )

    EXPECTED = {"adenorm", "acrossfade", "crop", "subtitles", "amovie"}

    def test_the_ffmpeg_7_layout_parses(self):
        self.assertEqual(_parse_filter_list(self.SEVEN), self.EXPECTED)

    def test_the_ffmpeg_8_layout_parses(self):
        """Two flag characters instead of three; this is what returned nothing."""
        self.assertEqual(_parse_filter_list(self.EIGHT), self.EXPECTED)

    def test_the_legend_is_not_mistaken_for_filters(self):
        for parsed in (_parse_filter_list(self.SEVEN), _parse_filter_list(self.EIGHT)):
            self.assertNotIn("=", parsed)
            self.assertNotIn("Timeline", parsed)

    def test_nothing_is_invented_from_an_empty_listing(self):
        self.assertEqual(_parse_filter_list("Filters:\n  ------\n"), set())


@unittest.skipUnless(have_ffmpeg(), "ffmpeg is not installed")
class FilterDetectionTests(unittest.TestCase):
    """And against the ffmpeg that is actually installed, whichever that is."""

    #: In every build since ffmpeg 2. If one of these is missing, the parse is
    #: broken, not the build.
    UNIVERSAL = ("crop", "scale", "overlay", "format", "null", "anull",
                 "volume", "aresample", "concat", "trim", "atrim", "fps")

    def test_the_filters_every_build_has_are_found(self):
        missing = [f for f in self.UNIVERSAL if not has_filter(f)]
        self.assertEqual(missing, [], f"the parse lost filters this build has: {missing}")

    def test_the_listing_is_not_almost_empty(self):
        self.assertGreater(len(available_filters()), 100,
                           "a near-empty set makes require_filter refuse every render")

    def test_nothing_detected_is_invented(self):
        """Cross-checked against a different ffmpeg command, so a parser that
        picks up description words fails here rather than looking plausible."""
        import random

        sample = random.Random(0).sample(sorted(available_filters()), 15)
        for name in sample:
            with self.subTest(filter=name):
                probe = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-h", f"filter={name}"],
                    capture_output=True, text=True,
                )
                self.assertNotIn("Unknown filter", probe.stdout + probe.stderr)

    def test_an_absent_filter_reads_as_absent(self):
        self.assertFalse(has_filter("no_such_filter_exists_here"))

    def test_an_unreadable_listing_does_not_become_a_refusal(self):
        """The listing has been emptied twice by ffmpeg changing its table.

        Both times the failure mode was the same shape: a parse that reads
        nothing looks identical to a build that has nothing. Refusing on that
        basis breaks a working install, so an unknown answer must let the
        render proceed and leave the verdict to ffmpeg."""
        from aicut.media import ffmpeg_util

        with mock.patch.object(ffmpeg_util, "available_filters", return_value=frozenset()):
            self.assertFalse(ffmpeg_util.filters_known())
            self.assertFalse(ffmpeg_util.filter_missing("subtitles"),
                             "an unreadable listing was treated as a missing filter")
            ffmpeg_util.require_filter(
                "subtitles", needed_for="a render that must not be blocked by a parse bug",
                install_hint="",
            )

    def test_a_filter_known_to_be_absent_is_still_refused(self):
        from aicut.media import ffmpeg_util
        from aicut.errors import RenderError

        with mock.patch.object(ffmpeg_util, "available_filters",
                               return_value=frozenset({"crop", "scale"})):
            self.assertTrue(ffmpeg_util.filter_missing("subtitles"))
            with self.assertRaises(RenderError) as raised:
                ffmpeg_util.require_filter(
                    "subtitles", needed_for="captions", install_hint="install libass")
            self.assertIn("install libass", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
