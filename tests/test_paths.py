"""Paths as they actually arrive: Korean names, spaces, brackets, relative form.

A Korean streamer's file is called 방송_2026-08-19 [하이라이트].mkv and sits in a
folder with a space in it. Every one of those characters passes through an
ffmpeg filter string, an ASS filename and an edit plan that is read back later
from a different directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.llm import get_producer
from aicut.media.ffmpeg_util import has_filter, have_ffmpeg
from aicut.media.stt import TranscriptFileTranscriber
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State
from aicut.render.editplan import EditPlan

AWKWARD_DIR = "한글 폴더 (test)"
AWKWARD_FILE = "방송_2026-08-19 [하이라이트].mkv"


def _psnr(a: Path, b: Path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b), "-filter_complex", "psnr", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for token in result.stderr.split():
        if token.startswith("average:"):
            return float(token.split(":")[1])
    raise AssertionError("ffmpeg printed no PSNR")


def _frame(video: Path, at_sec: float, out: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-ss", f"{at_sec}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=1920:1080,crop=1920:216:0:820", "-y", str(out)],
        check=True,
    )
    return out


@unittest.skipUnless(have_ffmpeg(), "ffmpeg is not installed")
class AwkwardPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls._tmp.name) / AWKWARD_DIR
        cls.root.mkdir(parents=True)
        cls.source = cls.root / AWKWARD_FILE
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=24",
            "-f", "lavfi", "-i", "sine=frequency=320:duration=24",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.source),
        ], check=True)

        cls.transcript = cls.root / "대본 파일.json"
        segments = []
        for start, text in ((2, "토너먼트 결승 시작"), (10, "토너먼트 상대가 강하다"),
                            (18, "토너먼트 결국 우승했다")):
            words = text.split()
            step = 4 / len(words)
            segments.append({
                "start": start, "end": start + 4, "text": text, "speaker": "HOST",
                "words": [{"word": w, "start": start + i * step, "end": start + (i + 1) * step}
                          for i, w in enumerate(words)],
            })
        cls.transcript.write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")

        cls.workspace = cls.root / "작업 공간"
        cls.store = Store(cls.workspace / "aicut.db")
        profile = CalibrationProfile.load().with_overrides({
            "scan.pass1_window_sec": 8, "scan.pass1_frame_interval_sec": 4,
            "situation.min_segment_sec": 8,
            "render.video.preset": "ultrafast", "render.video.crf": 35,
        }, measured=[])

        # Submit with a *relative* path from inside the folder, the way a person
        # would after dragging the file in.
        cwd = os.getcwd()
        os.chdir(cls.root)
        try:
            pipeline = Pipeline(cls.store, profile, get_producer("mock"), workspace=cls.workspace)
            cls.project = pipeline.submit(AWKWARD_FILE)
            cls.result = pipeline.run(
                cls.project, transcriber=TranscriptFileTranscriber(cls.transcript)
            )
        finally:
            os.chdir(cwd)
        cls.project_dir = cls.workspace / cls.project.project_id

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls._tmp.cleanup()

    def test_the_run_completes_through_awkward_paths(self):
        self.assertIs(self.result.final_state, State.REVIEW_PENDING, self.result.report.get("error", ""))
        self.assertTrue(self.result.episodes)

    def test_the_plan_stores_an_absolute_source_path(self):
        """16장 re-runs the render alone, possibly from elsewhere; a relative
        path would resolve against whatever directory that happened to be."""
        plan_path = next((self.project_dir / "plans").glob("*.json"))
        plan = EditPlan.load(plan_path)
        self.assertTrue(Path(plan.source_path).is_absolute())
        self.assertTrue(Path(plan.source_path).exists())
        self.assertIn("[하이라이트]", plan.source_path)

    def test_rendering_from_another_directory_still_finds_the_source(self):
        from aicut.pipeline import rendering
        from aicut.pipeline.context import RunContext

        episode = self.result.episodes[0]
        plan_path = self.project_dir / "plans" / f"{episode.episode_id}.json"
        ctx = RunContext(
            project=self.store.get_project(self.project.project_id), store=self.store,
            profile=CalibrationProfile.load().with_overrides(
                {"render.video.preset": "ultrafast", "render.video.crf": 35}, measured=[]),
            producer=get_producer("mock"), workspace=self.workspace,
        )
        cwd = os.getcwd()
        os.chdir(tempfile.gettempdir())
        try:
            rendering.render_episode(ctx, episode, plan_path=plan_path)
        finally:
            os.chdir(cwd)
        self.assertTrue(Path(episode.output_mp4_path).exists())

    def test_a_build_without_libass_still_produces_the_video_and_says_so(self):
        """Homebrew's plain ffmpeg bottle has no libass, so no 'subtitles' filter.

        Losing a whole episode over captions would be the wrong trade: render
        without them, keep the .ass beside the output, and report the departure
        (2.6) instead of shipping a quietly caption-less video.
        """
        from aicut.pipeline import rendering
        from aicut.pipeline.context import RunContext

        episode = self.store.episodes(self.project.project_id)[0]
        ctx = RunContext(
            project=self.store.get_project(self.project.project_id), store=self.store,
            profile=CalibrationProfile.load().with_overrides(
                {"render.video.preset": "ultrafast", "render.video.crf": 35}, measured=[]),
            producer=get_producer("mock"), workspace=self.workspace,
        )
        with mock.patch.object(rendering, "filter_missing", return_value=True):
            rendering.render_episode(ctx, episode)

        self.assertTrue(Path(episode.output_mp4_path).exists(), "the video was lost over captions")
        degraded = ctx.report.get("degraded", [])
        self.assertEqual(len(degraded), 1, "the departure was not reported")
        self.assertEqual(degraded[0]["reason"], "no_subtitles_filter")
        self.assertTrue(Path(degraded[0]["subtitle_file"]).exists(),
                        "the subtitle file must survive for a later re-render")
        self.assertIn("NOT burned in", episode.notes)

        # Re-rendering must not stack the same note on the episode.
        with mock.patch.object(rendering, "filter_missing", return_value=True):
            rendering.render_episode(ctx, episode)
        self.assertEqual(episode.notes.count("NOT burned in"), 1)

    @unittest.skipUnless(has_filter("subtitles"), "this ffmpeg was built without libass")
    def test_subtitles_burn_in_despite_the_brackets_and_spaces(self):
        """The ASS path goes into an ffmpeg filter string, where ':' and quotes
        are syntax. A silently unescaped path renders a video with no captions."""
        episode = next(e for e in self.result.episodes if e.subtitles)
        plan = EditPlan.load(self.project_dir / "plans" / f"{episode.episode_id}.json")
        output = Path(episode.output_mp4_path)
        self.assertTrue(output.exists())

        line = plan.subtitles[0]
        cut = min(plan.cuts, key=lambda c: c.sequence_order)
        source_at = cut.source_start_sec + line.start_sec + 0.3

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            burned = _psnr(
                _frame(output, line.start_sec + 0.3, Path(tmp) / "out.png"),
                _frame(Path(plan.source_path), source_at, Path(tmp) / "src.png"),
            )
        self.assertLess(burned, 25.0, "no caption appeared where the plan puts one")

    def test_the_ass_file_is_written_next_to_the_project_with_its_text_intact(self):
        episode = next(e for e in self.result.episodes if e.subtitles)
        ass = self.project_dir / "subtitles" / f"{episode.episode_id}.ass"
        self.assertTrue(ass.exists())
        text = ass.read_text(encoding="utf-8")
        self.assertIn("토너먼트", text)
        self.assertIn("[Events]", text)


if __name__ == "__main__":
    unittest.main()
