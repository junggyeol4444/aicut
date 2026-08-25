"""STT adapters (20.1, 20.2).

The models cannot run here - one wants a GPU, and the model weights are a
download away - but everything around them can be checked: that word timings
survive, that a segment with no speech is dropped, that speakers come from the
track layout as 5.2 says, and that the transcript written is the transcript the
rest of the system reads back.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aicut.media.probe import AudioTrack, MediaInfo
from aicut.media.stt import (
    FasterWhisperTranscriber,
    TranscriptFileTranscriber,
    speaker_reliability,
    utterances_from_whisperx,
    write_transcript,
)
from aicut.models import UNKNOWN_SPEAKER


def _word(text, start, end, probability=0.9):
    return SimpleNamespace(word=text, start=start, end=end, probability=probability)


def _segment(start, end, text, words=(), avg_logprob=-0.2):
    return SimpleNamespace(start=start, end=end, text=text, words=list(words), avg_logprob=avg_logprob)


def _fake_model(segments, language="ko"):
    return SimpleNamespace(
        transcribe=lambda *args, **kwargs: (list(segments), SimpleNamespace(language=language))
    )


def _media(*roles: str) -> MediaInfo:
    return MediaInfo(
        path="/f.mkv", duration_sec=100, video_codec="h264",
        audio_tracks=[AudioTrack(index=i + 1, channels=2, title=r, role=r) for i, r in enumerate(roles)],
    )


class FasterWhisperTests(unittest.TestCase):
    def test_word_timings_survive_the_conversion(self):
        """9장 measures gaps between words; without timings there is no pacing."""
        model = _fake_model([
            _segment(1.0, 3.0, " 안녕하세요 여러분 ",
                     [_word(" 안녕하세요", 1.0, 1.8), _word(" 여러분", 1.9, 3.0)]),
        ])
        utterances = FasterWhisperTranscriber(model=model).transcribe("/f.mkv", _media("mic"))

        self.assertEqual(len(utterances), 1)
        self.assertEqual(utterances[0].text, "안녕하세요 여러분")
        self.assertEqual([w["word"] for w in utterances[0].words], ["안녕하세요", "여러분"])
        self.assertEqual(utterances[0].words[0]["start"], 1.0)

    def test_an_empty_segment_is_dropped(self):
        model = _fake_model([_segment(4.0, 5.0, "   "), _segment(6.0, 7.0, "말했다", [_word("말했다", 6.0, 7.0)])])
        self.assertEqual(len(FasterWhisperTranscriber(model=model).transcribe("/f.mkv", None)), 1)

    def test_a_word_without_a_start_is_skipped_rather_than_faked(self):
        model = _fake_model([
            _segment(1.0, 2.0, "hi", [_word("hi", 1.0, 2.0), SimpleNamespace(word="?", start=None, end=None)]),
        ])
        utterances = FasterWhisperTranscriber(model=model).transcribe("/f.mkv", None)
        self.assertEqual(len(utterances[0].words), 1)

    def test_a_single_mic_track_names_the_speaker(self):
        """5.2: with one mic there is nothing to diarise."""
        model = _fake_model([_segment(0, 1, "hi", [_word("hi", 0, 1)])])
        utterances = FasterWhisperTranscriber(model=model).transcribe("/f.mkv", _media("mic"))
        self.assertEqual(utterances[0].speaker, "HOST")
        self.assertEqual(speaker_reliability(utterances), 1.0)

    def test_a_call_track_leaves_the_speaker_unknown(self):
        model = _fake_model([_segment(0, 1, "hi", [_word("hi", 0, 1)])])
        utterances = FasterWhisperTranscriber(model=model).transcribe("/f.mkv", _media("mic", "call"))
        self.assertEqual(utterances[0].speaker, UNKNOWN_SPEAKER)
        self.assertEqual(speaker_reliability(utterances), 0.0)

    def test_word_timestamps_are_always_requested(self):
        seen = {}

        def transcribe(path, **kwargs):
            seen.update(kwargs)
            return ([], SimpleNamespace(language="en"))

        FasterWhisperTranscriber(model=SimpleNamespace(transcribe=transcribe)).transcribe("/f.mkv", None)
        self.assertTrue(seen["word_timestamps"], "word timings are not optional for this pipeline")

    def test_a_missing_library_says_what_to_do(self):
        from aicut.errors import AicutError
        import sys

        saved = sys.modules.pop("faster_whisper", None)
        sys.modules["faster_whisper"] = None      # force the ImportError path
        try:
            with self.assertRaises(AicutError) as raised:
                FasterWhisperTranscriber().transcribe("/f.mkv", None)
            self.assertIn("--transcript", str(raised.exception))
        finally:
            sys.modules.pop("faster_whisper", None)
            if saved is not None:
                sys.modules["faster_whisper"] = saved


class TranscriptRoundTripTests(unittest.TestCase):
    def test_what_is_written_is_what_is_read_back(self):
        model = _fake_model([
            _segment(1.0, 3.0, "첫 문장", [_word("첫", 1.0, 1.4), _word("문장", 1.5, 3.0)]),
            _segment(10.0, 12.0, "두 번째", [_word("두", 10.0, 10.5), _word("번째", 10.6, 12.0)]),
        ])
        original = FasterWhisperTranscriber(model=model).transcribe("/f.mkv", _media("mic"))

        with tempfile.TemporaryDirectory() as tmp:
            path = write_transcript(original, Path(tmp) / "out.json")
            reloaded = TranscriptFileTranscriber(path).transcribe()

        self.assertEqual([u.text for u in reloaded], [u.text for u in original])
        self.assertEqual([u.speaker for u in reloaded], ["HOST", "HOST"])
        self.assertEqual(len(reloaded[0].words), 2)
        self.assertEqual(reloaded[1].start_sec, 10.0)

    def test_the_written_shape_is_the_one_the_loader_expects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_transcript(
                FasterWhisperTranscriber(model=_fake_model([
                    _segment(0, 1, "hi", [_word("hi", 0, 1)])
                ])).transcribe("/f.mkv", None),
                Path(tmp) / "t.json",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("segments", data)
        self.assertEqual(set(data["segments"][0]) >= {"start", "end", "text", "words"}, True)
        self.assertEqual(len(utterances_from_whisperx(data)), 1)

    def test_segments_come_back_in_time_order(self):
        data = {"segments": [
            {"start": 30, "end": 31, "text": "later"},
            {"start": 5, "end": 6, "text": "earlier"},
        ]}
        self.assertEqual([u.text for u in utterances_from_whisperx(data)], ["earlier", "later"])


if __name__ == "__main__":
    unittest.main()
