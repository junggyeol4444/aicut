"""Speech to text and speaker attribution.

Word-level timestamps are a hard requirement (20.1): the pacing layer measures
gaps *between words*, and the subtitle layer needs per-word timing for emphasis.

Speaker attribution follows 5.2: with a multi-track recording, the track already
tells you who is talking, and diarisation is only needed for a track that
carries two or more people (typically the call track). 16장 then says a failed
or wrong-count diarisation must not stop the run - tag the speaker UNKNOWN,
carry on, and disable speaker-dependent staging.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from aicut.errors import AicutError
from aicut.media.probe import MediaInfo
from aicut.models import UNKNOWN_SPEAKER, Utterance

log = logging.getLogger(__name__)


class Transcriber(ABC):
    @abstractmethod
    def transcribe(self, path: str, media: MediaInfo) -> list[Utterance]:
        ...


class TranscriptFileTranscriber(Transcriber):
    """Load an existing transcript instead of running STT.

    Used when STT has already been run elsewhere (a GPU box, a previous run) and
    for tests. Accepts WhisperX-shaped JSON: ``{"segments": [{start, end, text,
    speaker, words: [...]}]}``.
    """

    def __init__(self, transcript_path: str | Path, track: str = "mic"):
        self.transcript_path = Path(transcript_path)
        self.track = track

    def transcribe(self, path: str = "", media: MediaInfo | None = None) -> list[Utterance]:
        """Path and media are ignored: the transcript is the source of truth here."""
        data = json.loads(self.transcript_path.read_text(encoding="utf-8"))
        return utterances_from_whisperx(data, track=self.track)


def utterances_from_whisperx(data: dict[str, Any], track: str = "mic") -> list[Utterance]:
    out: list[Utterance] = []
    for seg in data.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append(
            Utterance(
                start_sec=float(seg.get("start", 0.0)),
                end_sec=float(seg.get("end", 0.0)),
                text=text,
                speaker=seg.get("speaker") or UNKNOWN_SPEAKER,
                track=seg.get("track", track),
                words=[
                    {"word": w.get("word", ""), "start": w.get("start"), "end": w.get("end"), "score": w.get("score")}
                    for w in seg.get("words", [])
                    if w.get("start") is not None
                ],
                confidence=seg.get("score"),
            )
        )
    out.sort(key=lambda u: u.start_sec)
    return out


class WhisperXTranscriber(Transcriber):
    """WhisperX with per-track handling and optional diarisation.

    Diarisation models are gated on HuggingFace and need approval before first
    use (20.2). If the model is unavailable or the run fails, this degrades to
    UNKNOWN speakers rather than failing the project (16장).
    """

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str | None = None,
        hf_token: str | None = None,
        diarize: bool = True,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.hf_token = hf_token
        self.diarize = diarize

    def transcribe(self, path: str, media: MediaInfo) -> list[Utterance]:  # pragma: no cover - needs GPU stack
        try:
            import whisperx
        except ImportError as exc:
            # This is the default backend, so this is the most likely first run
            # anyone has. The other two guard their imports; this one did not,
            # and a bare ModuleNotFoundError traceback is not an answer.
            raise AicutError(
                "whisperx is not installed, and it is the default STT backend.\n"
                "  pip install 'aicut[stt]'            - install it (wants a GPU)\n"
                "  --backend faster-whisper            - runs on a CPU\n"
                "  --backend pocketsphinx              - no download, no GPU, poor accuracy\n"
                "  --transcript <file>                 - use a transcript made elsewhere\n"
                "  --no-stt                            - skip STT entirely"
            ) from exc

        audio = whisperx.load_audio(path)
        model = whisperx.load_model(self.model_size, self.device, compute_type=self.compute_type, language=self.language)
        result = model.transcribe(audio)
        align_model, meta = whisperx.load_align_model(language_code=result["language"], device=self.device)
        result = whisperx.align(result["segments"], align_model, meta, audio, self.device, return_char_alignments=False)

        speech_tracks = [t for t in media.audio_tracks if t.role in ("mic", "call", "mixed", "unknown")]
        needs_diarization = self.diarize and any(t.needs_diarization for t in speech_tracks)
        if needs_diarization:
            try:
                pipeline = whisperx.DiarizationPipeline(use_auth_token=self.hf_token, device=self.device)
                result = whisperx.assign_word_speakers(pipeline(audio), result)
            except Exception as exc:
                log.warning(
                    "diarisation failed (%s); continuing with UNKNOWN speakers and speaker-dependent staging off (16장)",
                    exc,
                )
        elif len(speech_tracks) == 1 and speech_tracks[0].role == "mic":
            for seg in result.get("segments", []):
                seg.setdefault("speaker", "HOST")
        return utterances_from_whisperx(result)


class FasterWhisperTranscriber(Transcriber):
    """faster-whisper, which runs on a CPU.

    WhisperX gives the best word timings but assumes a GPU, and 20.2 leaves the
    hardware open. This is the backend for a machine without one: slower, no
    diarisation of its own, but it produces the word-level timestamps the pacing
    and subtitle layers require, which is the part that cannot be given up.

    Speakers come from the track layout (5.2) rather than from diarisation here:
    with a multi-track recording that is already the better answer, and with a
    single mixed track everything is tagged UNKNOWN and speaker-dependent
    staging switches itself off (16장).
    """

    def __init__(
        self,
        model_size: str = "base",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = True,
        model=None,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self._model = model

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - optional dep
            raise AicutError(
                "faster-whisper is not installed; pip install faster-whisper, or supply a "
                "transcript with --transcript"
            ) from exc
        self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, path: str, media: MediaInfo | None = None) -> list[Utterance]:
        model = self._load()
        segments, info = model.transcribe(
            path,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            word_timestamps=True,       # required: pacing measures gaps between words
        )
        speaker = _speaker_from_tracks(media)
        log.info("faster-whisper: language=%s", getattr(info, "language", "?"))
        return [u for u in (_utterance_from_segment(seg, speaker) for seg in segments) if u is not None]


def _speaker_from_tracks(media: MediaInfo | None) -> str:
    """A single mic track means one known speaker; anything else is UNKNOWN (5.2)."""
    if media is None:
        return UNKNOWN_SPEAKER
    speech = [t for t in media.audio_tracks if t.role in ("mic", "call", "mixed", "unknown")]
    if len(speech) == 1 and speech[0].role == "mic":
        return "HOST"
    return UNKNOWN_SPEAKER


def _utterance_from_segment(segment, speaker: str) -> Utterance | None:
    text = (getattr(segment, "text", "") or "").strip()
    if not text:
        return None
    words = [
        {"word": (w.word or "").strip(), "start": w.start, "end": w.end, "score": getattr(w, "probability", None)}
        for w in (getattr(segment, "words", None) or [])
        if getattr(w, "start", None) is not None
    ]
    return Utterance(
        start_sec=float(segment.start),
        end_sec=float(segment.end),
        text=text,
        speaker=speaker,
        words=words,
        confidence=getattr(segment, "avg_logprob", None),
    )


class PocketSphinxTranscriber(Transcriber):
    """CMU PocketSphinx, whose acoustic model ships inside the package.

    Accuracy is poor and it is English-only, so this is not the backend to
    produce a channel's transcripts with. It earns its place because it needs
    no download and no GPU: it is the one engine that can be run anywhere,
    which makes it the way to check that a real recogniser's output - word
    timings and all - flows through the pipeline, rather than a fixture that
    was shaped to fit.

    Use ``faster-whisper`` or WhisperX for anything real (20.1).
    """

    def __init__(self, *, sample_rate: int = 16000, decoder=None):
        self.sample_rate = sample_rate
        self._decoder = decoder

    def _load(self):
        if self._decoder is not None:
            return self._decoder
        try:
            from pocketsphinx import Decoder
        except ImportError as exc:  # pragma: no cover - optional dep
            raise AicutError("pocketsphinx is not installed; pip install pocketsphinx") from exc
        self._decoder = Decoder(logfn=os.devnull)
        return self._decoder

    #: How much audio to hand the decoder at a time. Small enough that the
    #: buffer never dominates memory, large enough that the per-call overhead
    #: is noise: 10s of 16 kHz mono 16-bit is 320 kB.
    CHUNK_SEC = 10

    def transcribe(self, path: str, media: MediaInfo | None = None) -> list[Utterance]:
        decoder = self._load()
        speaker = _speaker_from_tracks(media)

        # Streamed, not read whole. Six hours of 16 kHz mono PCM is 691 MB, and
        # reading it into one bytes object cost 1.6 GB of resident memory before
        # decoding had even started - on a program that is meant to run on a
        # desktop (22.1) beside a game capture. The decoder takes it in pieces.
        words: list[dict] = []
        offset_sec = 0.0
        for block in self._stream_mono_pcm(path):
            decoder.start_utt()
            decoder.process_raw(block, False, True)
            decoder.end_utt()
            words.extend(
                {
                    "word": seg.word,
                    "start": offset_sec + seg.start_frame / 100.0,
                    "end": offset_sec + seg.end_frame / 100.0,
                }
                for seg in decoder.seg()
                if seg.word not in _SPHINX_NOISE
            )
            # The decoder's frame counter restarts with each utterance, so the
            # block's own start has to be carried or every timestamp collapses
            # onto the first ten seconds.
            offset_sec += len(block) / (self.sample_rate * 2)

        return group_words_into_utterances(words, speaker=speaker)

    def _stream_mono_pcm(self, path: str):
        """Yield 16 kHz mono PCM in blocks, without ever holding all of it."""
        from aicut.media.ffmpeg_util import require_ffmpeg
        import subprocess

        require_ffmpeg()
        block_bytes = int(self.CHUNK_SEC * self.sample_rate) * 2
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-v", "error", "-i", path,
             "-ac", "1", "-ar", str(self.sample_rate), "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            while True:
                block = proc.stdout.read(block_bytes)
                if not block:
                    break
                yield block
        finally:
            proc.stdout.close()
            stderr = proc.stderr.read()
            proc.stderr.close()
            code = proc.wait()
        if code != 0:
            raise AicutError(
                f"could not extract audio from {path}: "
                f"{stderr.decode('utf-8', 'replace')[-300:]}"
            )


_SPHINX_NOISE = {"<s>", "</s>", "<sil>", "[SPEECH]", "[NOISE]", "(NULL)", "++UH++", "++UM++"}


def group_words_into_utterances(
    words: list[dict],
    *,
    speaker: str = UNKNOWN_SPEAKER,
    max_gap_sec: float = 0.8,
) -> list[Utterance]:
    """Turn a flat word stream into utterances, splitting on pauses.

    A recogniser that returns words without sentence boundaries still has to
    produce the units the rest of the system reads. The pause between words is
    the only boundary available, and it is the same signal 9장 judges - which is
    why the threshold here is deliberately loose: this splits transcript lines,
    it does not decide what a beat is.
    """
    utterances: list[Utterance] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(_clean_word(w["word"]) for w in current).strip()
        if text:
            utterances.append(Utterance(
                start_sec=float(current[0]["start"]),
                end_sec=float(current[-1]["end"]),
                text=text,
                speaker=speaker,
                words=[{"word": _clean_word(w["word"]), "start": w["start"], "end": w["end"]}
                       for w in current],
            ))
        current.clear()

    for word in sorted(words, key=lambda w: float(w["start"])):
        if current and float(word["start"]) - float(current[-1]["end"]) > max_gap_sec:
            flush()
        current.append(word)
    flush()
    return utterances


def _clean_word(word: str) -> str:
    """Drop the pronunciation-variant suffix Sphinx appends: ``the(2)``."""
    return word.split("(")[0].strip()


def write_transcript(utterances: list[Utterance], path: str | Path) -> Path:
    """Save in the WhisperX-shaped JSON the rest of the system reads."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "segments": [
            {
                "start": u.start_sec, "end": u.end_sec, "text": u.text,
                "speaker": u.speaker, "track": u.track, "words": u.words,
            }
            for u in utterances
        ]
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def speaker_reliability(utterances: list[Utterance]) -> float:
    """Share of speech that carries a real speaker tag.

    The planner reads this to decide whether speaker-dependent staging
    (cross-cutting between reaction shots, per-speaker subtitle styles) is
    allowed at all - 16장's degradation rule expressed as a number.
    """
    if not utterances:
        return 0.0
    known = sum(1 for u in utterances if u.speaker and u.speaker != UNKNOWN_SPEAKER)
    return known / len(utterances)
