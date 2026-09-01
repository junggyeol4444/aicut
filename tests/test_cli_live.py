"""CLI paths that need real media: run, render, benchmark, review, upload queue."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from aicut.cli import main
from aicut.db.store import Store
from aicut.media.ffmpeg_util import have_ffmpeg


def run(*argv: str) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


@unittest.skipUnless(have_ffmpeg(), "ffmpeg is not installed")
class CliLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.dir = Path(cls._tmp.name)
        cls.workspace = cls.dir / "ws"
        cls.source = cls.dir / "stream.mkv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=40",
            "-f", "lavfi", "-i",
            "sine=frequency=320:duration=40,volume='if(between(t,15,21),0.0,0.25)':eval=frame",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.source),
        ], check=True)

        cls.transcript = cls.dir / "transcript.json"
        segments = []
        for start, text in ((2, "the tournament final begins now"),
                            (10, "he nearly lost the tournament there"),
                            (24, "and he wins the tournament"),
                            (33, "that is the tournament finished")):
            words = text.split()
            step = 5 / len(words)
            segments.append({
                "start": start, "end": start + 5, "text": text, "speaker": "HOST",
                "words": [{"word": w, "start": start + i * step, "end": start + (i + 1) * step}
                          for i, w in enumerate(words)],
            })
        cls.transcript.write_text(json.dumps({"segments": segments}), encoding="utf-8")

        cls.profile = cls.dir / "short.json"
        cls.profile.write_text(json.dumps({
            "_meta": {"name": "cli-test"},
            "scan": {"pass1_window_sec": 10, "pass1_frame_interval_sec": 5},
            "situation": {"min_segment_sec": 10},
            "render": {"video": {"preset": "ultrafast", "crf": 35}},
        }), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _base(self) -> list[str]:
        return ["--workspace", str(self.workspace), "--profile", str(self.profile)]

    def test_run_to_plan_then_render_then_review_then_queue(self):
        """The operating flow of 15.1, end to end, through the CLI."""
        code, out = run(*self._base(), "run", str(self.source),
                        "--transcript", str(self.transcript), "--no-render")
        self.assertEqual(code, 0, out)
        self.assertIn("PLANNING", out)
        self.assertIn("17.5", out, "the run must declare the unmeasured values it used")

        store = Store(self.workspace / "aicut.db")
        try:
            project = store.list_projects()[-1]
            episodes = store.episodes(project.project_id)
            self.assertTrue(episodes)
            episode = episodes[0]
        finally:
            store.close()

        plan_path = self.workspace / project.project_id / "plans" / f"{episode.episode_id}.json"
        code, described = run("plan", str(plan_path))
        self.assertEqual(code, 0)
        self.assertIn("structure", described)

        code, rendered = run(*self._base(), "render", str(plan_path))
        self.assertEqual(code, 0, rendered)
        # The command may print warnings after the path (a build that cannot
        # burn captions, say), so take the line, not the tail of the output.
        line = next(l for l in rendered.splitlines() if l.startswith("rendered "))
        output = Path(line[len("rendered "):].strip())
        self.assertTrue(output.exists())
        self.assertGreater(output.stat().st_size, 0)

        code, _ = run(*self._base(), "review", episode.episode_id, "approve", "--reviewer", "junggyeol")
        self.assertEqual(code, 0)

        store = Store(self.workspace / "aicut.db")
        try:
            self.assertEqual(store.get_episode(episode.episode_id).review_status, "approved")
        finally:
            store.close()

        code, listed = run("--workspace", str(self.workspace), "status", project.project_id)
        self.assertEqual(code, 0)
        self.assertIn("PLANNING", listed)

        code, candidates = run("--workspace", str(self.workspace), "candidates", project.project_id)
        self.assertEqual(code, 0)
        self.assertIn("why:", candidates, "a decision must be shown with its reasoning (15.4)")

    def test_benchmark_reports_a_realtime_factor(self):
        """R3 and 20.2 ask for this to be measured, so it is a command."""
        code, out = run("--workspace", str(self.workspace), "benchmark", str(self.source))
        self.assertEqual(code, 0, out)
        self.assertIn("realtime", out)
        self.assertIn("six-hour broadcast", out)
        self.assertIn("silence detection", out)

    def test_benchmark_refuses_a_source_it_cannot_use(self):
        """An unusable source exits non-zero with a message, not a traceback."""
        broken = self.dir / "broken.mkv"
        broken.write_bytes(b"")
        code, _ = run("--workspace", str(self.workspace), "benchmark", str(broken))
        self.assertEqual(code, 1)

    def test_run_on_a_source_with_no_audio_fails_with_the_reason(self):
        silent = self.dir / "silent.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=5",
            "-c:v", "libx264", "-preset", "ultrafast", str(silent),
        ], check=True)

        code, out = run("--workspace", str(self.dir / "ws2"), "run", str(silent), "--no-stt", "--no-render")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)


if __name__ == "__main__":
    unittest.main()
