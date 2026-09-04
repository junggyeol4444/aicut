"""The tension curve.

A weighted mix of loudness, peak, speech rate and laughter, normalised against
*this broadcast's own* distribution rather than an absolute dB figure - a value
of 0.8 means "loud for this stream", which is the only meaning that survives a
mic change (17.1, 17.4 step 1).

The weights and the high/low marks come from the profile. They are marked
provisional in the shipped default and stay that way until 17.4 measures them.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field

from aicut.config import CalibrationProfile
from aicut.models import Utterance


@dataclass
class TensionCurve:
    """Tension sampled once per frame_sec, plus the scale it was normalised on."""

    times: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    frame_sec: float = 1.0
    scale: tuple[float, float] = (-60.0, -10.0)

    def at(self, sec: float) -> float:
        if not self.times:
            return 0.0
        i = bisect_left(self.times, sec)
        if i <= 0:
            return self.values[0]
        if i >= len(self.values):
            return self.values[-1]
        return self.values[i]

    def peak(self, start_sec: float, end_sec: float) -> float:
        window = [v for t, v in zip(self.times, self.values) if start_sec <= t <= end_sec]
        return max(window) if window else 0.0

    def mean(self, start_sec: float, end_sec: float) -> float:
        window = [v for t, v in zip(self.times, self.values) if start_sec <= t <= end_sec]
        return sum(window) / len(window) if window else 0.0

    def held_low_for(self, start_sec: float, end_sec: float, low: float) -> bool:
        window = [v for t, v in zip(self.times, self.values) if start_sec <= t <= end_sec]
        return bool(window) and all(v <= low for v in window)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def build_tension_curve(
    rms: list[tuple[float, float]],
    utterances: list[Utterance],
    profile: CalibrationProfile,
    *,
    laughter: list[tuple[float, float]] | None = None,
    frame_sec: float = 1.0,
) -> TensionCurve:
    """Combine the measured signals into one 0..1 curve.

    Args:
        rms: ``(time_sec, level_db)`` from :func:`aicut.media.audio.rms_envelope`.
        utterances: used for speech rate (words per second around each frame).
        profile: supplies ``tension.weights`` and ``tension.smoothing_window_sec``.
        laughter: optional ``(time_sec, 0..1)`` from a laughter/scream detector.
            Absent, its weight is redistributed instead of silently scoring zero.
    """
    if not rms:
        return TensionCurve(frame_sec=frame_sec)

    weights = dict(profile.get("tension.weights"))
    smoothing = profile.get_float("tension.smoothing_window_sec")

    levels = sorted(level for _, level in rms)
    floor, ceiling = _percentile(levels, 0.05), _percentile(levels, 0.95)
    span = max(1e-6, ceiling - floor)

    if laughter is None:
        weights.pop("laughter", None)
    total_weight = sum(weights.values()) or 1.0

    word_times = _word_times(utterances)
    laughter_map = {round(t): v for t, v in (laughter or [])}

    times: list[float] = []
    values: list[float] = []
    for i, (at, level) in enumerate(rms):
        loud = _clamp((level - floor) / span)
        window = [lv for _, lv in rms[max(0, i - 2) : i + 3]]
        peak = _clamp((max(window) - floor) / span) if window else loud
        rate = _speech_rate(word_times, at, frame_sec)
        parts = {"rms": loud, "peak": peak, "speech_rate": rate, "laughter": laughter_map.get(round(at), 0.0)}
        score = sum(weights.get(k, 0.0) * v for k, v in parts.items()) / total_weight
        times.append(at)
        values.append(_clamp(score))

    return TensionCurve(
        times=times,
        values=_smooth(values, max(1, int(round(smoothing / max(frame_sec, 1e-6))))),
        frame_sec=frame_sec,
        scale=(floor, ceiling),
    )


def _word_times(utterances: list[Utterance]) -> list[float]:
    times: list[float] = []
    for u in utterances:
        if u.words:
            times.extend(float(w["start"]) for w in u.words if w.get("start") is not None)
        else:
            span = max(0.1, u.end_sec - u.start_sec)
            count = max(1, len(u.text.split()))
            times.extend(u.start_sec + span * i / count for i in range(count))
    times.sort()
    return times


def _speech_rate(word_times: list[float], at: float, frame_sec: float, *, window_sec: float = 4.0) -> float:
    if not word_times:
        return 0.0
    lo = bisect_left(word_times, at - window_sec / 2)
    hi = bisect_left(word_times, at + window_sec / 2)
    words_per_sec = (hi - lo) / window_sec
    # ~5 words/sec is around the top of sustained excited speech; above that the
    # signal saturates rather than continuing to grow.
    return _clamp(words_per_sec / 5.0)


def _smooth(values: list[float], window: int) -> list[float]:
    if window <= 1:
        return values
    out = []
    half = window // 2
    for i in range(len(values)):
        chunk = values[max(0, i - half) : i + half + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))
