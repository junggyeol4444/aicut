"""A synthetic broadcast for offline tests.

Two events an hour apart, one of which is called back to much later - the shape
that non-linear reconstruction (2.4) and the event graph (5.4) exist for - plus a
long stretch of dead air that pacing should cut and a stunned pause it should
keep.
"""

from __future__ import annotations

from aicut.analysis.tension import TensionCurve
from aicut.media.audio import Silence
from aicut.media.probe import AudioTrack, MediaInfo
from aicut.media.vision import MotionSample
from aicut.models import Utterance

DURATION = 3600.0


def utterances() -> list[Utterance]:
    def u(start: float, text: str, speaker: str = "HOST", length: float = 4.0) -> Utterance:
        words = text.split()
        step = length / max(1, len(words))
        return Utterance(
            start_sec=start,
            end_sec=start + length,
            text=text,
            speaker=speaker,
            words=[{"word": w, "start": start + i * step, "end": start + (i + 1) * step} for i, w in enumerate(words)],
        )

    return [
        # event 1: the boss fight, first mention
        u(30, "okay this boss has killed me eleven times now"),
        u(60, "the boss keeps one shotting me at half health"),
        u(120, "i am going to beat this boss today i swear"),
        # unrelated filler
        u(900, "anyway someone asked what i had for lunch"),
        u(960, "it was just a sandwich nothing exciting about lunch"),
        # event 1 continues: the win
        u(1800, "wait wait wait the boss is almost dead"),
        u(1830, "i beat the boss i actually beat the boss", "HOST", 5.0),
        # event 2: a guest arrives and reacts to the boss story
        u(2400, "hey welcome to the stream", "HOST"),
        u(2410, "did you really beat that boss earlier", "GUEST"),
        u(2420, "he beat the boss after eleven deaths", "GUEST"),
        u(2430, "eleven deaths is honestly impressive", "GUEST"),
        # dead air follows, then a callback
        u(3300, "someone in chat just called me a boss slayer"),
    ]


def silences() -> list[Silence]:
    return [
        Silence(1835.5, 1837.0),      # stunned pause right after the win
        Silence(2600.0, 2640.0),      # away from desk
        Silence(130.0, 131.0),        # short beat mid-sentence
    ]


def tension() -> TensionCurve:
    times = [float(t) for t in range(0, int(DURATION), 5)]
    values = []
    for t in times:
        if 1790 <= t <= 1840:
            values.append(0.9)        # the win
        elif 2400 <= t <= 2450:
            values.append(0.65)       # the guest reacting
        elif 2500 <= t <= 2700:
            values.append(0.05)       # away
        else:
            values.append(0.3)
    return TensionCurve(times=times, values=values, frame_sec=5.0, scale=(-58.0, -20.0))


def motion() -> list[MotionSample]:
    return [
        MotionSample(at_sec=float(t), score=0.01 if 1830 <= t <= 1840 or 2500 <= t <= 2700 else 0.2)
        for t in range(0, int(DURATION), 5)
    ]


def media() -> MediaInfo:
    return MediaInfo(
        path="/fixture/stream.mkv",
        duration_sec=DURATION,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        audio_tracks=[
            AudioTrack(index=1, channels=2, title="mic", role="mic"),
            AudioTrack(index=2, channels=2, title="discord call", role="call"),
            AudioTrack(index=3, channels=2, title="game", role="game"),
        ],
    )
