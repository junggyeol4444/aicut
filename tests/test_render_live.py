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
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.media.ffmpeg_util import have_ffmpeg
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
        cls._tmp = tempfile.TemporaryDirectory()
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

    def test_an_empty_timeline_is_refused_rather_than_producing_a_broken_file(self):
        from aicut.errors import RenderError

        with self.assertRaises(RenderError):
            self._render(Episode(project_id="p", timeline=[]), "empty")


if __name__ == "__main__":
    unittest.main()
