"""The whole pipeline against real media (5장 through 11장).

Every other pipeline test feeds pre-measured signals so it can run anywhere.
This one hands the system an actual file and lets it probe, detect silence,
measure loudness, sample frames, render, extract thumbnails and write metadata
for itself. It is the test that would have caught anything the measurement
layer gets wrong about a real container.

Skipped without ffmpeg.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.llm import get_producer
from aicut.media.ffmpeg_util import have_ffmpeg
from aicut.media.probe import probe
from aicut.media.stt import TranscriptFileTranscriber
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State

SOURCE_SEC = 48


def _write_source(path: Path) -> None:
    """A clip with engineered loud moments and two engineered silences.

    Loud at 10-13 and 30-32, silent at 18-24 and 36-44: the pacing layer should
    find the silences, and the burst detector should find the loud stretches
    that carry no words.
    """
    volume = (
        "if(between(t,10,13)+between(t,30,32),1.0,"
        "if(between(t,18,24)+between(t,36,44),0.0,0.25))"
    )
    subprocess.run([
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=10:duration={SOURCE_SEC}",
        "-f", "lavfi", "-i",
        f"sine=frequency=320:duration={SOURCE_SEC},volume='{volume}':eval=frame",
        "-f", "lavfi", "-i", f"anoisesrc=duration={SOURCE_SEC}:color=brown:amplitude=0.03",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-metadata:s:a:0", "title=mic", "-metadata:s:a:1", "title=game",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def _write_transcript(path: Path) -> None:
    segments = []

    def add(start: float, end: float, text: str, speaker: str = "HOST") -> None:
        words = text.split()
        step = (end - start) / len(words)
        segments.append({
            "start": start, "end": end, "text": text, "speaker": speaker,
            "words": [{"word": w, "start": start + i * step, "end": start + (i + 1) * step}
                      for i, w in enumerate(words)],
        })

    add(2, 8, "okay the tournament final is starting and i am nervous about this")
    add(14, 17, "he almost lost the tournament right there")
    add(25, 29, "back to the tournament bracket for the last round")
    add(33, 35, "and he wins the whole tournament")
    add(45, 47, "that is the tournament done", "GUEST")
    path.write_text(json.dumps({"segments": segments}), encoding="utf-8")


@unittest.skipUnless(have_ffmpeg(), "ffmpeg is not installed")
class LivePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.source = cls.dir / "stream.mkv"
        cls.transcript = cls.dir / "transcript.json"
        _write_source(cls.source)
        _write_transcript(cls.transcript)

        cls.store = Store(cls.dir / "aicut.db")
        # Windows sized for a 48-second clip rather than a six-hour broadcast,
        # and the fastest encode: neither changes what is being tested.
        cls.profile = CalibrationProfile.load().with_overrides({
            "scan.pass1_window_sec": 12,
            "scan.pass1_frame_interval_sec": 3,
            "situation.min_segment_sec": 10,
            "render.video.preset": "ultrafast",
            "render.video.crf": 35,
        }, measured=[])
        pipeline = Pipeline(cls.store, cls.profile, get_producer("mock"), workspace=cls.dir / "ws")
        cls.project = pipeline.submit(str(cls.source), length_hint_sec=20)
        cls.result = pipeline.run(
            cls.project, transcriber=TranscriptFileTranscriber(cls.transcript), sample_frames=True
        )
        cls.project_dir = cls.dir / "ws" / cls.project.project_id

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls._tmp.cleanup()

    def test_the_run_reaches_the_review_gate(self):
        self.assertIs(self.result.final_state, State.REVIEW_PENDING, self.result.report.get("error", ""))
        self.assertTrue(self.result.episodes)

    def test_the_multitrack_layout_is_read_from_the_container(self):
        """5.2: the track tells you who is talking, so diarisation is not needed."""
        media = probe(str(self.source))
        self.assertTrue(media.is_multitrack)
        self.assertEqual(media.track_by_role("mic").index, 1)
        self.assertFalse(media.track_by_role("mic").needs_diarization)
        self.assertAlmostEqual(media.duration_sec, SOURCE_SEC, delta=1.0)

    def test_the_engineered_silences_were_measured(self):
        from aicut.pipeline.context import SignalBundle

        signals = SignalBundle.load(self.project_dir / "signals.json")
        self.assertTrue(signals.silences)
        found = [(s.start_sec, s.end_sec) for s in signals.silences if s.duration > 3]
        self.assertTrue(
            any(18 <= start <= 20 for start, _ in found),
            f"the silence engineered at 18-24s was not found: {found}",
        )

    def test_loud_wordless_moments_were_detected_as_bursts(self):
        from aicut.pipeline.context import SignalBundle

        signals = SignalBundle.load(self.project_dir / "signals.json")
        self.assertTrue(signals.rms, "no RMS envelope was measured off the real file")
        self.assertTrue(signals.bursts, "no vocal burst found in a clip with two loud wordless stretches")

    def test_every_rendered_file_matches_its_plan(self):
        from aicut.render.editplan import EditPlan
        from aicut.render.timeline import Timeline

        for episode in self.result.episodes:
            with self.subTest(episode=episode.episode_id):
                output = Path(episode.output_mp4_path)
                self.assertTrue(output.exists())
                plan = EditPlan.load(self.project_dir / "plans" / f"{episode.episode_id}.json")
                expected = Timeline.from_cuts(plan.cuts).duration
                probed = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(output)],
                    capture_output=True, text=True, check=True,
                )
                self.assertAlmostEqual(float(probed.stdout), expected, delta=0.5)

    def test_packaging_produced_thumbnails_and_metadata(self):
        for episode in self.result.episodes:
            with self.subTest(episode=episode.episode_id):
                self.assertTrue(episode.thumbnail_candidates, "no thumbnail candidate was extracted")
                for thumb in episode.thumbnail_candidates:
                    self.assertTrue(Path(thumb).exists())
                meta = json.loads(
                    (self.project_dir / "metadata" / f"{episode.episode_id}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(len(meta["titles"]), 3)
                self.assertIn("chapters", meta)

    def test_the_report_names_the_unmeasured_parameters_it_relied_on(self):
        report = json.loads((self.project_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["final_state"], State.REVIEW_PENDING.value)
        self.assertTrue(report["provisional_parameters_used"])
        self.assertIn("silence.level_db", report["provisional_parameters_used"])

    def test_nothing_is_published_by_the_run_itself(self):
        """11.3: the run stops at the gate; a person moves it past."""
        for episode in self.store.episodes(self.project.project_id):
            self.assertEqual(episode.review_status, "pending")


if __name__ == "__main__":
    unittest.main()
