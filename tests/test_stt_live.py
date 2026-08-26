"""A real recogniser, end to end (20.1, 20.2).

Every other STT test feeds a fixture shaped to fit. This one generates speech,
runs an actual recogniser over it, and pushes what comes out through the whole
pipeline. PocketSphinx is the engine because its acoustic model ships inside
the package: no download, no GPU, so it runs where WhisperX cannot. Its
accuracy is bad and beside the point - what is being checked is that a real
recogniser's output, with its word timings and its pause boundaries, is
something this system can actually work from.

Skipped unless pocketsphinx, espeak-ng and ffmpeg are all present.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from aicut.media.ffmpeg_util import have_ffmpeg


def _has(name: str) -> bool:
    return shutil.which(name) is not None


def _importable(name: str) -> bool:
    from importlib import util

    try:
        return util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


REQUIREMENTS = have_ffmpeg() and _has("espeak-ng") and _importable("pocketsphinx")

# Long pauses between three sentences: the recogniser has to find the words,
# and the grouping has to find the boundaries.
LINES = [
    "the tournament final is starting now and i am very nervous about this match",
    "he almost lost the tournament right there that was incredibly close",
    "and he wins the whole tournament what a finish to the match",
]
PAUSE_SEC = 4


@unittest.skipUnless(REQUIREMENTS, "needs ffmpeg, espeak-ng and pocketsphinx")
class RealRecogniserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.dir = Path(cls._tmp.name)

        parts = []
        for index, line in enumerate(LINES):
            spoken = cls.dir / f"line{index}.wav"
            subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(spoken), line], check=True)
            parts.append(spoken)

        silence = cls.dir / "silence.wav"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(PAUSE_SEC), str(silence),
        ], check=True)

        inputs: list[str] = []
        sequence = [parts[0], silence, parts[1], silence, parts[2]]
        for item in sequence:
            inputs += ["-i", str(item)]
        cls.speech = cls.dir / "speech.wav"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y", *inputs,
            "-filter_complex", f"{''.join(f'[{i}]' for i in range(len(sequence)))}"
                               f"concat=n={len(sequence)}:v=0:a=1[a]",
            "-map", "[a]", "-ar", "16000", "-ac", "1", str(cls.speech),
        ], check=True)

        cls.video = cls.dir / "stream.mkv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=21",
            "-i", str(cls.speech), "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.video),
        ], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _transcribe(self):
        from aicut.media.stt import PocketSphinxTranscriber

        return PocketSphinxTranscriber().transcribe(str(self.speech))

    def test_a_real_recogniser_produces_word_timings(self):
        """Not a fixture: these timings came out of an acoustic model."""
        utterances = self._transcribe()
        self.assertTrue(utterances)
        words = [w for u in utterances for w in u.words]
        self.assertGreater(len(words), 15)
        for word in words:
            self.assertIsNotNone(word["start"])
            self.assertGreaterEqual(word["end"], word["start"])
        self.assertLess(words[0]["start"], 2.0)

    def test_the_engineered_pauses_become_utterance_boundaries(self):
        """9장 reads gaps between words; the grouping has to find them first."""
        utterances = self._transcribe()
        self.assertEqual(len(utterances), len(LINES),
                         f"expected one utterance per spoken line, got {len(utterances)}")
        for earlier, later in zip(utterances, utterances[1:]):
            self.assertGreater(later.start_sec - earlier.end_sec, PAUSE_SEC * 0.6,
                               "an utterance boundary did not land on the silence")

    def test_pronunciation_variant_suffixes_are_stripped(self):
        """Sphinx writes `the(2)`; that must not reach a subtitle."""
        for utterance in self._transcribe():
            self.assertNotIn("(", utterance.text)
            for word in utterance.words:
                self.assertNotIn("(", word["word"])

    def test_the_transcript_round_trips_into_the_pipeline_format(self):
        from aicut.media.stt import TranscriptFileTranscriber, write_transcript

        original = self._transcribe()
        path = write_transcript(original, self.dir / "transcript.json")
        reloaded = TranscriptFileTranscriber(path).transcribe()
        self.assertEqual([u.text for u in reloaded], [u.text for u in original])
        self.assertEqual(len(reloaded[0].words), len(original[0].words))

    def test_the_whole_pipeline_runs_on_what_the_recogniser_produced(self):
        """No fixture anywhere: speech in, rendered video out."""
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.media.stt import PocketSphinxTranscriber
        from aicut.pipeline.runner import Pipeline
        from aicut.pipeline.states import State

        workspace = self.dir / "ws"
        store = Store(workspace / "aicut.db")
        try:
            profile = CalibrationProfile.load().with_overrides({
                "scan.pass1_window_sec": 7,
                "scan.pass1_frame_interval_sec": 3,
                "situation.min_segment_sec": 7,
                "render.video.preset": "ultrafast",
                "render.video.crf": 35,
            }, measured=[])
            pipeline = Pipeline(store, profile, get_producer("mock"), workspace=workspace)
            project = pipeline.submit(str(self.video))
            result = pipeline.run(project, transcriber=PocketSphinxTranscriber())

            self.assertIs(result.final_state, State.REVIEW_PENDING,
                          result.report.get("error", ""))
            self.assertEqual(result.report["signals"]["utterances"], len(LINES))
            self.assertTrue(result.episodes)
            for episode in result.episodes:
                self.assertTrue(Path(episode.output_mp4_path).exists())
                self.assertGreater(Path(episode.output_mp4_path).stat().st_size, 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
