"""Loop B: learning from source-and-finished-video pairs (12.3 B, MVP 4).

This is the loop that separates a producer from a rule engine. Loops A and C say
what tends to work on the platform and what worked afterwards; only B shows the
actual editing decision - out of six hours, *this* is what a person kept, this is
what they dropped, this is the order they put it in, this is what they repeated
and what they emphasised.

The same alignment doubles as the calibration dataset of 17.2, so this module
also produces the labelled span pairs the sweep scores against - one piece of
work serving both purposes, as 17.2 notes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from aicut.db.store import Store
from aicut.llm import Producer
from aicut.models import Utterance

log = logging.getLogger(__name__)


@dataclass
class AlignedSpan:
    """A source span and where it ended up in the human's finished video."""

    source_start_sec: float
    source_end_sec: float
    output_start_sec: float | None = None
    output_end_sec: float | None = None
    kept: bool = True
    order_changed: bool = False
    repeated: int = 1
    text: str = ""

    @property
    def source_duration(self) -> float:
        return max(0.0, self.source_end_sec - self.source_start_sec)

    @property
    def compression(self) -> float:
        """How much shorter the span got. 1.0 means untouched, 0 means cut."""
        if not self.kept or self.output_end_sec is None or self.output_start_sec is None:
            return 0.0
        if self.source_duration <= 0:
            return 0.0
        return (self.output_end_sec - self.output_start_sec) / self.source_duration


@dataclass
class Alignment:
    """The full mapping between one source and one human-made output."""

    source_ref: str
    output_ref: str
    spans: list[AlignedSpan] = field(default_factory=list)

    @property
    def kept_spans(self) -> list[AlignedSpan]:
        return [s for s in self.spans if s.kept]

    @property
    def keep_ratio(self) -> float:
        total = sum(s.source_duration for s in self.spans)
        kept = sum(s.source_duration for s in self.kept_spans)
        return kept / total if total else 0.0

    def reordered(self) -> bool:
        outputs = [s.output_start_sec for s in self.kept_spans if s.output_start_sec is not None]
        sources = [s.source_start_sec for s in self.kept_spans if s.output_start_sec is not None]
        ranked = [x for _, x in sorted(zip(outputs, sources))]
        return ranked != sorted(ranked)


def align_by_transcript(
    source_utterances: Sequence[Utterance],
    output_utterances: Sequence[Utterance],
    *,
    min_overlap: float = 0.6,
) -> Alignment:
    """Match the finished video's speech back to the source's speech.

    Text is the anchor because the output has been cut, sped up, reordered and
    overlaid, but the words are still the words. Anything in the source that no
    output line matches is what the editor threw away - which is the more
    informative half of the signal.
    """
    def norm(text: str) -> list[str]:
        return [w.lower().strip(".,!?\"'") for w in text.split() if w.strip(".,!?\"'")]

    output_tokens = [(u, norm(u.text)) for u in output_utterances]
    used: dict[int, int] = {}
    spans: list[AlignedSpan] = []

    for source in source_utterances:
        tokens = set(norm(source.text))
        best_index, best_score = None, 0.0
        for i, (_, other) in enumerate(output_tokens):
            if not other or not tokens:
                continue
            overlap = len(tokens & set(other)) / max(1, min(len(tokens), len(set(other))))
            if overlap > best_score:
                best_index, best_score = i, overlap

        if best_index is not None and best_score >= min_overlap:
            match = output_tokens[best_index][0]
            used[best_index] = used.get(best_index, 0) + 1
            spans.append(AlignedSpan(
                source_start_sec=source.start_sec,
                source_end_sec=source.end_sec,
                output_start_sec=match.start_sec,
                output_end_sec=match.end_sec,
                kept=True,
                repeated=used[best_index],
                text=source.text,
            ))
        else:
            spans.append(AlignedSpan(
                source_start_sec=source.start_sec,
                source_end_sec=source.end_sec,
                kept=False,
                text=source.text,
            ))

    alignment = Alignment(source_ref="", output_ref="", spans=spans)
    reordered = alignment.reordered()
    for span in alignment.kept_spans:
        span.order_changed = reordered
    return alignment


def learn(
    producer: Producer,
    store: Store,
    alignment: Alignment,
    *,
    source_ref: str,
    output_ref: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn one alignment into stated editing rules and store the pair."""
    payload = {
        "source_ref": source_ref,
        "output_ref": output_ref,
        "keep_ratio": round(alignment.keep_ratio, 4),
        "reordered": alignment.reordered(),
        "kept": [
            {
                "source": [s.source_start_sec, s.source_end_sec],
                "output": [s.output_start_sec, s.output_end_sec],
                "compression": round(s.compression, 3),
                "repeated": s.repeated,
                "text": s.text[:200],
            }
            for s in alignment.kept_spans
        ],
        "dropped": [
            {"source": [s.source_start_sec, s.source_end_sec], "text": s.text[:200]}
            for s in alignment.spans if not s.kept
        ],
        "context": context or {},
    }
    analysis = producer.compare_source_output(payload)
    analysis["measured"] = {
        "keep_ratio": payload["keep_ratio"],
        "reordered": payload["reordered"],
        "kept_spans": len(alignment.kept_spans),
        "dropped_spans": len(alignment.spans) - len(alignment.kept_spans),
    }
    store.save_source_output_pair(source_ref, output_ref, analysis)
    return analysis
