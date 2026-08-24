"""Source time <-> output time.

An episode's cuts come from anywhere in the source and in any order (2.4), and
pacing removes spans from inside them (9.3). So "when does this line appear in
the finished video" is a real computation, and both the subtitle writer and the
chapter list depend on getting it right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from aicut.models import Cut


@dataclass
class Segment:
    """One continuous piece of source that survives into the output."""

    cut_index: int
    sequence_order: int
    source_start_sec: float
    source_end_sec: float
    out_start_sec: float

    @property
    def duration(self) -> float:
        return max(0.0, self.source_end_sec - self.source_start_sec)

    @property
    def out_end_sec(self) -> float:
        return self.out_start_sec + self.duration


class Timeline:
    """The ordered segment list of a finished episode."""

    def __init__(self, segments: list[Segment]):
        self.segments = segments

    @classmethod
    def from_cuts(cls, cuts: Sequence[Cut]) -> "Timeline":
        segments: list[Segment] = []
        clock = 0.0
        for index, cut in enumerate(sorted(cuts, key=lambda c: c.sequence_order)):
            for start, end in cut.kept_spans():
                segment = Segment(
                    cut_index=index,
                    sequence_order=cut.sequence_order,
                    source_start_sec=start,
                    source_end_sec=end,
                    out_start_sec=clock,
                )
                segments.append(segment)
                clock += segment.duration
        return cls(segments)

    @property
    def duration(self) -> float:
        return self.segments[-1].out_end_sec if self.segments else 0.0

    def to_output(self, source_sec: float, *, sequence_order: int | None = None) -> float | None:
        """Where a source moment lands in the output.

        A source second can appear more than once - repeating a scene is a
        legitimate editing choice (4.3) - so pass ``sequence_order`` to say which
        occurrence you mean. Returns None when that moment was cut.
        """
        for segment in self.segments:
            if sequence_order is not None and segment.sequence_order != sequence_order:
                continue
            if segment.source_start_sec <= source_sec <= segment.source_end_sec:
                return segment.out_start_sec + (source_sec - segment.source_start_sec)
        return None

    def to_source(self, out_sec: float) -> tuple[int, float] | None:
        """Inverse map: output second -> (sequence_order, source second)."""
        for segment in self.segments:
            if segment.out_start_sec <= out_sec <= segment.out_end_sec:
                return (segment.sequence_order, segment.source_start_sec + (out_sec - segment.out_start_sec))
        return None

    def cut_boundaries(self) -> list[float]:
        """Output times where one cut hands over to the next (chapter candidates)."""
        seen: set[int] = set()
        out: list[float] = []
        for segment in self.segments:
            if segment.sequence_order not in seen:
                seen.add(segment.sequence_order)
                out.append(segment.out_start_sec)
        return out
