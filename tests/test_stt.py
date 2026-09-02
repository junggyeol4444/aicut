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
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from aicut.media.probe import AudioTrack, MediaInfo
from aicut.media.stt import (
    FasterWhisperTranscriber,
    TranscriptFileTranscriber,
    WhisperXTranscriber,
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

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write_transcript(original, Path(tmp) / "out.json")
            reloaded = TranscriptFileTranscriber(path).transcribe()

        self.assertEqual([u.text for u in reloaded], [u.text for u in original])
        self.assertEqual([u.speaker for u in reloaded], ["HOST", "HOST"])
        self.assertEqual(len(reloaded[0].words), 2)
        self.assertEqual(reloaded[1].start_sec, 10.0)

    def test_the_written_shape_is_the_one_the_loader_expects(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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


class MissingBackendTests(unittest.TestCase):
    """A missing optional dependency is an instruction, not a traceback.

    WhisperX is the default backend and the one least likely to be installed,
    so "no module named whisperx" with a stack trace is the most common first
    run this program has. The other two backends already guarded their imports.
    """

    def _message(self, transcriber, module: str) -> str:
        import builtins

        from aicut.errors import AicutError
        from aicut.media.probe import MediaInfo

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == module or name.startswith(module + "."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", refuse):
            with self.assertRaises(AicutError) as raised:
                transcriber.transcribe("broadcast.mkv", MediaInfo(path="b.mkv", duration_sec=10.0))
        return str(raised.exception)

    def test_whisperx_missing_names_every_way_out(self):
        message = self._message(WhisperXTranscriber(), "whisperx")
        for alternative in ("aicut[stt]", "faster-whisper", "pocketsphinx",
                            "--transcript", "--no-stt"):
            with self.subTest(alternative=alternative):
                self.assertIn(alternative, message)

    def test_faster_whisper_missing_is_also_an_instruction(self):
        message = self._message(FasterWhisperTranscriber(), "faster_whisper")
        self.assertIn("faster-whisper is not installed", message)


class SilentBlockTests(unittest.TestCase):
    """A block with no speech in it must not end the run.

    PocketSphinx returns None from `seg()` rather than an empty sequence when a
    block decodes to nothing. Every fixture here was speech end to end, so this
    never came up until a real film went through: music and sound effects with
    no dialogue, which is what most of a game broadcast sounds like.

        TypeError: 'NoneType' object is not iterable
    """

    class _MuteDecoder:
        """Decodes everything to nothing, the way silence and music do."""

        def __init__(self):
            self.blocks = 0

        def start_utt(self):
            pass

        def process_raw(self, block, a, b):
            self.blocks += 1

        def end_utt(self):
            pass

        def seg(self):
            return None

    def test_a_block_that_recognises_nothing_yields_no_words(self):
        from aicut.media.stt import PocketSphinxTranscriber

        decoder = self._MuteDecoder()
        transcriber = PocketSphinxTranscriber(decoder=decoder)
        with mock.patch.object(
            PocketSphinxTranscriber, "_stream_mono_pcm",
            return_value=iter([b"\x00" * 32000, b"\x00" * 32000]),
        ):
            self.assertEqual(transcriber.transcribe("silent.wav"), [])
        self.assertEqual(decoder.blocks, 2, "both blocks should still have been offered")

    def test_speech_after_a_silent_block_keeps_its_real_timestamps(self):
        """The offset must advance across blocks that produced nothing."""
        from aicut.media.stt import PocketSphinxTranscriber

        class _Seg:
            def __init__(self, word, start, end):
                self.word, self.start_frame, self.end_frame = word, start, end

        class _LateDecoder(self._MuteDecoder):
            def seg(self):
                # Nothing in the first block, one word in the second.
                return None if self.blocks < 2 else [_Seg("hello", 50, 100)]

        transcriber = PocketSphinxTranscriber(decoder=_LateDecoder())
        block = b"\x00" * (16000 * 2 * PocketSphinxTranscriber.CHUNK_SEC)
        with mock.patch.object(
            PocketSphinxTranscriber, "_stream_mono_pcm", return_value=iter([block, block]),
        ):
            utterances = transcriber.transcribe("late.wav")
        self.assertEqual(len(utterances), 1)
        word = utterances[0].words[0]
        self.assertAlmostEqual(word["start"], PocketSphinxTranscriber.CHUNK_SEC + 0.5, places=2)
