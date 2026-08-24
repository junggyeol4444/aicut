"""Laughter and scream detection (9.1, 5.2 오디오).

The tension curve wants a laughter term and 5.1's first pass wants to know when
something loud happened that was not talking. Both are the same question: **is
this a vocal burst?**

No classifier is trained here. The detection uses two things the pipeline already
measures, and the reasoning is the whole method:

* the moment is loud relative to *this broadcast's own* speech level, and
* almost nothing was transcribed under it.

Laughter, screams and shouted reactions are loud and produce few or no words.
Excited talking is loud and produces many. That separation is coarse - a long
shouted sentence will be missed, a loud cough will be caught - and it is stated
here rather than hidden, because 9.4 requires this to be scored against human
edits like every other judgement.

A real laughter classifier drops in behind :class:`VocalBurstDetector` without
touching the callers.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Sequence

from aicut.config import CalibrationProfile
from aicut.models import Utterance

log = logging.getLogger(__name__)


@dataclass
class VocalBurst:
    start_sec: float
    end_sec: float
    intensity: float          # 0..1, how far above the speech baseline
    word_density: float = 0.0  # words per second transcribed underneath

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


class VocalBurstDetector(ABC):
    @abstractmethod
    def detect(
        self,
        rms: Sequence[tuple[float, float]],
        utterances: Sequence[Utterance],
        profile: CalibrationProfile,
    ) -> list[VocalBurst]:
        ...

    def as_signal(self, bursts: Sequence[VocalBurst], *, frame_sec: float = 1.0) -> list[tuple[float, float]]:
        """``(time_sec, 0..1)`` pairs for the tension curve's laughter term."""
        signal: list[tuple[float, float]] = []
        for burst in bursts:
            at = burst.start_sec
            while at < burst.end_sec:
                signal.append((at, burst.intensity))
                at += frame_sec
        return signal


class LoudNonSpeechDetector(VocalBurstDetector):
    """Loud stretches with little or no transcribed speech underneath.

    Thresholds come from the profile and are relative: the loudness line is a
    percentile of this broadcast's own level distribution, so a quiet mic and a
    hot one land in the same place (17.4 step 1's reasoning applied to a second
    signal).
    """

    def detect(
        self,
        rms: Sequence[tuple[float, float]],
        utterances: Sequence[Utterance],
        profile: CalibrationProfile,
    ) -> list[VocalBurst]:
        if not rms:
            return []
        percentile = profile.get_float("laughter.loud_percentile")
        min_duration = profile.get_float("laughter.min_duration_sec")
        max_density = profile.get_float("laughter.max_word_density")
        merge_gap = profile.get_float("laughter.merge_gap_sec")

        levels = sorted(level for _, level in rms if level > -120)
        if not levels:
            return []
        index = min(len(levels) - 1, max(0, int(percentile * (len(levels) - 1))))
        loud_line = levels[index]
        median = levels[len(levels) // 2]
        if loud_line <= median:
            # The percentile landed on the broadcast's ordinary level, which
            # happens when the loud moments are rarer than (1 - percentile).
            # A line at or below the middle is not marking loudness, so move up
            # to the next distinct level that is.
            above = [level for level in levels if level > loud_line]
            if not above:
                return []                   # a flat recording holds no burst
            loud_line = above[0]
        # Intensity is measured against the broadcast's ordinary level, not
        # against the loudness line: with the line at the top of a narrow
        # distribution, every burst would otherwise score zero.
        ceiling = levels[-1]
        span = max(1e-6, ceiling - median)

        frame_sec = (rms[1][0] - rms[0][0]) if len(rms) > 1 else 1.0
        word_times = _word_times(utterances)

        runs: list[tuple[float, float, float]] = []      # start, end, peak level
        start: float | None = None
        peak = -120.0
        for at, level in rms:
            if level >= loud_line:
                start = at if start is None else start
                peak = max(peak, level)
            elif start is not None:
                runs.append((start, at, peak))
                start, peak = None, -120.0
        if start is not None:
            runs.append((start, rms[-1][0] + frame_sec, peak))

        # The envelope cannot resolve a gap shorter than one frame, so a
        # one-frame dip counts as continuous however small merge_gap is set.
        gap = max(merge_gap, frame_sec)
        merged: list[tuple[float, float, float]] = []
        for run in runs:
            if merged and run[0] - merged[-1][1] <= gap:
                previous = merged[-1]
                merged[-1] = (previous[0], run[1], max(previous[2], run[2]))
            else:
                merged.append(run)

        bursts: list[VocalBurst] = []
        for run_start, run_end, run_peak in merged:
            duration = run_end - run_start
            if duration < min_duration:
                continue
            words = bisect_right(word_times, run_end) - bisect_left(word_times, run_start)
            density = words / duration if duration else 0.0
            if density > max_density:
                continue                    # loud and wordy: that is shouting a sentence
            bursts.append(VocalBurst(
                start_sec=run_start,
                end_sec=run_end,
                intensity=min(1.0, max(0.0, (run_peak - median) / span)),
                word_density=round(density, 3),
            ))
        return bursts


def _word_times(utterances: Sequence[Utterance]) -> list[float]:
    times: list[float] = []
    for utterance in utterances:
        if utterance.words:
            times.extend(float(w["start"]) for w in utterance.words if w.get("start") is not None)
        else:
            span = max(0.1, utterance.end_sec - utterance.start_sec)
            count = max(1, len(utterance.text.split()))
            times.extend(utterance.start_sec + span * i / count for i in range(count))
    times.sort()
    return times


def build_detector(name: str = "loud_non_speech") -> VocalBurstDetector | None:
    if name in ("loud_non_speech", "default"):
        return LoudNonSpeechDetector()
    if name in ("", "none", "off"):
        return None
    log.warning("unknown vocal burst detector %r; running without one", name)
    return None
