"""Degenerate and damaged sources (16장, 5.2).

A production tool is handed broken files: an interrupted recording, a screen
capture with the mic muted, a stray audio export. Each of these has to fail at
the door with a reason, or - worse - be caught before it wastes hours and then
dies somewhere unrelated.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from aicut.media.ffmpeg_util import have_ffmpeg
from aicut.media.probe import UnusableSource, probe, verify_tail


@unittest.skipUnless(have_ffmpeg(), "ffmpeg is not installed")
class BadInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.dir = Path(cls._tmp.name)

        cls.good = cls.dir / "good.mkv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=20",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.good),
        ], check=True)

        cls.empty = cls.dir / "empty.mkv"
        cls.empty.write_bytes(b"")

        cls.garbage = cls.dir / "garbage.mkv"
        cls.garbage.write_bytes(b"this is not a video file, it is a text file wearing a hat")

        # An interrupted recording: header intact, packets cut off.
        cls.truncated = cls.dir / "truncated.mkv"
        cls.truncated.write_bytes(cls.good.read_bytes()[: len(cls.good.read_bytes()) // 3])

        cls.silent_video = cls.dir / "no_audio.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=5",
            "-c:v", "libx264", "-preset", "ultrafast", str(cls.silent_video),
        ], check=True)

        cls.audio_only = cls.dir / "audio_only.m4a"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=5", "-c:a", "aac", str(cls.audio_only),
        ], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_missing_file_says_so(self):
        with self.assertRaises(UnusableSource) as raised:
            probe(str(self.dir / "not_here.mkv"))
        self.assertIn("no such file", str(raised.exception))

    def test_an_empty_file_is_an_input_problem_not_a_render_failure(self):
        """Calling this a RenderError would send the operator to the wrong place."""
        with self.assertRaises(UnusableSource):
            probe(str(self.empty))

    def test_a_garbage_file_is_refused(self):
        with self.assertRaises(UnusableSource):
            probe(str(self.garbage))

    def test_a_video_with_no_audio_is_refused_with_the_reason(self):
        """5.2: the passes read sound as well as picture."""
        media = probe(str(self.silent_video))
        with self.assertRaises(UnusableSource) as raised:
            media.validate()
        self.assertIn("no audio stream", str(raised.exception))

    def test_an_audio_only_file_is_refused(self):
        media = probe(str(self.audio_only))
        with self.assertRaises(UnusableSource) as raised:
            media.validate()
        self.assertIn("no video stream", str(raised.exception))

    def test_a_good_file_passes_both_checks(self):
        media = probe(str(self.good))
        self.assertEqual(media.validate(), [])
        self.assertIsNone(verify_tail(str(self.good), media.duration_sec))

    def test_a_truncated_file_is_caught_even_though_it_lies_about_its_length(self):
        """Matroska keeps the header, so the duration still reads full - and
        ffmpeg exits 0 on the missing tail. Only the absent frame gives it away."""
        media = probe(str(self.truncated))
        self.assertGreater(media.duration_sec, 15.0, "the container still claims the original length")
        problem = verify_tail(str(self.truncated), media.duration_sec)
        self.assertIsNotNone(problem, "a truncated source went undetected")
        self.assertIn("truncated", problem)

    def test_the_pipeline_records_the_warning_instead_of_dying_late(self):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Project
        from aicut.pipeline import parsing
        from aicut.pipeline.context import RunContext

        store = Store(self.dir / "warn.sqlite")
        try:
            project = store.create_project(Project(file_path=str(self.truncated)))
            ctx = RunContext(
                project=project, store=store, profile=CalibrationProfile.load(),
                producer=get_producer("mock"), workspace=self.dir / "ws",
            )
            parsing.run(ctx, None)
            self.assertTrue(ctx.report.get("source_warnings"))
            self.assertIn("truncated", ctx.report["source_warnings"][0])
        finally:
            store.close()

    def test_an_unusable_source_fails_the_run_with_its_reason(self):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.pipeline.runner import Pipeline
        from aicut.pipeline.states import State

        store = Store(self.dir / "fail.sqlite")
        try:
            pipeline = Pipeline(store, CalibrationProfile.load(), get_producer("mock"),
                                workspace=self.dir / "ws2")
            project = pipeline.submit(str(self.silent_video))
            result = pipeline.run(project, render=False)
            self.assertIs(result.final_state, State.FAILED)
            self.assertIn("no audio stream", store.state_log(project.project_id)[-1]["detail"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
