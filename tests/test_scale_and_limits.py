"""Six-hour scale, and the limits a local server still needs (R3, 15장).

R3 names processing time as the open risk; memory was never measured at all.
A six-hour broadcast is the design target, so the signal volume it produces is
built here at full size and pushed through the layers that hold it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from aicut.analysis.tension import build_tension_curve
from aicut.analysis.vocalburst import LoudNonSpeechDetector
from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import AicutError
from aicut.media.audio import Silence
from aicut.media.vision import MotionSample
from aicut.models import Project, Utterance
from aicut.pipeline.context import SignalBundle

SIX_HOURS = 6 * 3600


def _rss_mb() -> float | None:
    """Peak RSS in MB, or None where the platform will not say.

    `resource` is POSIX-only; on Windows the memory assertion skips rather than
    the whole module failing to import.
    """
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS reports bytes.
    return peak / 1024 if sys.platform != "darwin" else peak / (1024 * 1024)


def _six_hour_signals() -> tuple[list, list, list, list]:
    rms = [(float(t), -45.0 + (t % 37)) for t in range(SIX_HOURS)]
    motion = [MotionSample(float(t), 0.2) for t in range(0, SIX_HOURS, 5)]
    silences = [Silence(float(t), float(t) + 2.5) for t in range(0, SIX_HOURS, 60)]
    utterances = []
    for i in range(SIX_HOURS * 110 // 60 // 8):          # ~110 words a minute
        start = i * 4.4
        words = [{"word": f"w{j}", "start": start + j * 0.4, "end": start + (j + 1) * 0.4} for j in range(8)]
        utterances.append(Utterance(
            start, start + 3.5, " ".join(w["word"] for w in words), speaker="HOST", words=words,
        ))
    return rms, motion, silences, utterances


class SixHourScaleTests(unittest.TestCase):
    """Nothing here should be slow or large. The point is to know that, not hope."""

    @classmethod
    def setUpClass(cls):
        cls.profile = CalibrationProfile.load()
        cls.rms, cls.motion, cls.silences, cls.utterances = _six_hour_signals()

    def test_the_signal_volume_is_what_a_six_hour_broadcast_produces(self):
        self.assertEqual(len(self.rms), SIX_HOURS)
        self.assertGreater(len(self.utterances), 4000)
        self.assertGreater(sum(len(u.words) for u in self.utterances), 30000)

    def test_the_tension_curve_holds_the_whole_broadcast(self):
        curve = build_tension_curve(self.rms, self.utterances, self.profile)
        self.assertEqual(len(curve.values), len(self.rms))
        self.assertIsNotNone(curve.at(SIX_HOURS - 1))
        self.assertGreaterEqual(curve.peak(0, SIX_HOURS), curve.mean(0, SIX_HOURS))

    def test_burst_detection_scales_to_the_whole_broadcast(self):
        bursts = LoudNonSpeechDetector().detect(self.rms, self.utterances, self.profile)
        self.assertIsInstance(bursts, list)

    def test_the_signal_cache_stays_small_enough_to_reload(self):
        bundle = SignalBundle(
            tension=build_tension_curve(self.rms, [], self.profile),
            motion=self.motion, silences=self.silences, rms=self.rms,
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = bundle.save(Path(tmp) / "signals.json")
            megabytes = path.stat().st_size / 1e6
            reloaded = SignalBundle.load(path)
        self.assertLess(megabytes, 20, f"the signal cache is {megabytes:.1f} MB for one broadcast")
        self.assertEqual(len(reloaded.rms), len(self.rms))
        self.assertEqual(len(reloaded.silences), len(self.silences))

    def test_a_window_of_speech_is_read_without_loading_the_broadcast(self):
        """The first pass reads window by window; that must stay indexed."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                project = store.create_project(
                    Project(file_path="/six.mkv", duration_sec=SIX_HOURS)
                )
                store.replace_utterances(project.project_id, self.utterances)
                window = store.utterances(project.project_id, 10000, 10120)
                self.assertTrue(window)
                self.assertLess(len(window), 100)
                self.assertTrue(all(u.end_sec >= 10000 and u.start_sec <= 10120 for u in window))
                self.assertEqual(len(store.utterances(project.project_id)), len(self.utterances))
            finally:
                store.close()

    def test_peak_memory_stays_modest(self):
        """A desktop tool that needs gigabytes to hold one broadcast's signals
        is not a desktop tool."""
        peak = _rss_mb()
        if peak is None:
            self.skipTest("this platform does not report peak RSS")
        self.assertLess(peak, 1024, f"peak RSS reached {peak:.0f} MB")


class WorkspaceGuardTests(unittest.TestCase):
    def test_a_file_where_the_workspace_should_be_is_reported_clearly(self):
        from aicut.llm import get_producer
        from aicut.pipeline.context import RunContext

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            blocker = Path(tmp) / "workspace"
            blocker.write_text("not a directory", encoding="utf-8")
            store = Store()
            try:
                project = store.create_project(Project(file_path="/x.mkv"))
                ctx = RunContext(
                    project=project, store=store, profile=CalibrationProfile.load(),
                    producer=get_producer("mock"), workspace=blocker,
                )
                with self.assertRaises(AicutError) as raised:
                    _ = ctx.project_dir
                self.assertIn("workspace", str(raised.exception))
            finally:
                store.close()

    def test_the_ui_refuses_a_workspace_it_cannot_use(self):
        from aicut.ui.server import UiServer

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            blocker = Path(tmp) / "ws"
            blocker.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(AicutError):
                UiServer(blocker)


class RequestLimitTests(unittest.TestCase):
    """15장's server is local and unauthenticated; it still must not be trivial
    to exhaust from a stray client."""

    @classmethod
    def setUpClass(cls):
        from aicut.ui.server import serve

        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.httpd, cls.ui = serve(Path(cls._tmp.name), port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        import threading

        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.ui.close()
        cls._tmp.cleanup()

    def _post(self, body: bytes, *, content_length: str | None = None):
        request = urllib.request.Request(
            f"{self.base}/api/projects", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        if content_length is not None:
            request.add_unredirected_header("Content-Length", content_length)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        from aicut.ui.server import MAX_BODY_BYTES

        status, body = self._post(b"{}", content_length=str(MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)
        self.assertIn("limit", body["error"])

    def test_a_nonsense_content_length_is_a_bad_request_not_a_crash(self):
        status, body = self._post(b"{}", content_length="banana")
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", body["error"])

    def test_a_normal_body_still_works(self):
        status, body = self._post(json.dumps({"source": "/nope/missing.mkv"}).encode())
        self.assertEqual(status, 400)                 # the file does not exist
        self.assertIn("file not found", body["error"])


if __name__ == "__main__":
    unittest.main()
