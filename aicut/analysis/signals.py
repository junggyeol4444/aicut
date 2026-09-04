"""Screen-state labelling (5.3) and boundary *hints* (6.4).

The status of everything in this module is deliberately low. 5.3's labels are an
internal analysis signal and never an output category, and 6.4 demotes the whole
family of boundary detectors from "splitting rule" to "hint that narrows where to
look". Content boundaries are settled by the event graph (5.4) and the discovery
stage (6장) - never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from aicut.analysis.tension import TensionCurve
from aicut.config import CalibrationProfile
from aicut.media.vision import MotionSample, stillness
from aicut.models import SituationLabel, SituationSpan, UNKNOWN_SPEAKER, Utterance


@dataclass
class BoundaryHint:
    """A place worth *looking* for a boundary, with the reasons that raised it."""

    at_sec: float
    kinds: list[str] = field(default_factory=list)
    detail: dict[str, float] = field(default_factory=dict)

    @property
    def strength(self) -> int:
        return len(self.kinds)


# ---------------------------------------------------------------------------
# 5.3 screen state
# ---------------------------------------------------------------------------
def label_situations(
    duration_sec: float,
    utterances: Sequence[Utterance],
    motion: Sequence[MotionSample],
    profile: CalibrationProfile,
    *,
    face_ratio: Callable[[float, float], float] | None = None,
) -> list[SituationSpan]:
    """Cut the broadcast into spans that share a screen state.

    Evidence used: how many distinct speakers talk in the span, whether anyone
    talks at all, how much the screen moves, and - when a face detector is
    wired in - how much of the frame is the streamer's face. With no face
    signal the talk/gameplay distinction is left UNKNOWN rather than guessed,
    because a wrong label here would silently mislead the passes above it.
    """
    step = profile.get_float("situation.min_segment_sec")
    solo_face = profile.get_float("situation.face_ratio_solo_talk")
    multi_min = profile.get_int("situation.multi_person_min_speakers")
    away_motion = profile.get_float("situation.away_max_motion")

    spans: list[SituationSpan] = []
    at = 0.0
    while at < duration_sec:
        end = min(duration_sec, at + step)
        inside = [u for u in utterances if u.end_sec > at and u.start_sec < end]
        speakers = sorted({u.speaker for u in inside if u.speaker != UNKNOWN_SPEAKER})
        move = stillness(list(motion), at, end)
        face = face_ratio(at, end) if face_ratio else None

        if not inside and move <= away_motion:
            label = SituationLabel.AWAY
        elif len(speakers) >= multi_min:
            label = SituationLabel.MULTI_PERSON
        elif face is None:
            label = SituationLabel.UNKNOWN
        elif face >= solo_face:
            label = SituationLabel.SOLO_TALK
        else:
            label = SituationLabel.GAMEPLAY

        spans.append(SituationSpan(
            start_sec=at, end_sec=end, label=label, speakers=speakers,
            evidence={"motion": round(move, 4), "face_ratio": face, "utterances": len(inside)},
        ))
        at = end
    return _merge_adjacent(spans)


def _merge_adjacent(spans: list[SituationSpan]) -> list[SituationSpan]:
    merged: list[SituationSpan] = []
    for span in spans:
        if merged and merged[-1].label == span.label:
            prev = merged[-1]
            prev.end_sec = span.end_sec
            prev.speakers = sorted(set(prev.speakers) | set(span.speakers))
        else:
            merged.append(span)
    return merged


# ---------------------------------------------------------------------------
# topic drift
# ---------------------------------------------------------------------------
Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]


def lexical_similarity(a: str, b: str) -> float:
    """Jaccard over content words - the fallback when no embedder is supplied.

    It is weak on purpose and only ever feeds a hint. Pass an ``embedder`` to
    ``topic_shifts`` to replace it with real sentence embeddings.
    """
    wa = {w.strip(".,!?\"'").lower() for w in a.split() if len(w) > 1}
    wb = {w.strip(".,!?\"'").lower() for w in b.split() if len(w) > 1}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def topic_shifts(
    utterances: Sequence[Utterance],
    profile: CalibrationProfile,
    *,
    embedder: Embedder | None = None,
) -> list[float]:
    """Times where the conversation's vocabulary drops away from what came before."""
    window = profile.get_int("topic_shift.window_utterances")
    drop = profile.get_float("topic_shift.similarity_drop")
    if len(utterances) < window * 2:
        return []

    texts = [u.text for u in utterances]
    vectors = list(embedder(texts)) if embedder is not None else None

    shifts: list[float] = []
    for i in range(window, len(utterances) - window):
        if vectors is None:
            score = lexical_similarity(" ".join(texts[i - window : i]), " ".join(texts[i : i + window]))
        else:
            score = _cosine(
                _mean_vec(vectors[i - window : i]),
                _mean_vec(vectors[i : i + window]),
            )
        if score <= drop:
            shifts.append(utterances[i].start_sec)
    return _dedupe(shifts, min_gap=profile.get_float("situation.min_segment_sec"))


def _mean_vec(vectors: Iterable[Sequence[float]]) -> list[float]:
    rows = [list(v) for v in vectors]
    if not rows:
        return []
    return [sum(col) / len(rows) for col in zip(*rows)]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _dedupe(times: list[float], min_gap: float) -> list[float]:
    out: list[float] = []
    for t in times:
        if not out or t - out[-1] >= min_gap:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# 6.4 boundary hints
# ---------------------------------------------------------------------------
def boundary_hints(
    situations: Sequence[SituationSpan],
    tension: TensionCurve,
    shift_times: Sequence[float],
    profile: CalibrationProfile,
) -> list[BoundaryHint]:
    """Collect the three hint families of 6.4 and keep the places where they agree.

    A single hint is never enough to place a boundary; the profile's
    ``boundary_hints.min_hint_count`` says how many must coincide before the spot
    is even worth handing to discovery as a place to look.
    """
    hold = profile.get_float("boundary_hints.situation_hold_sec")
    floor_hold = profile.get_float("boundary_hints.tension_floor_hold_sec")
    low = profile.get_float("tension.low")
    min_hints = profile.get_int("boundary_hints.min_hint_count")
    grid = profile.get_float("situation.min_segment_sec")

    raw: dict[float, BoundaryHint] = {}

    def add(at: float, kind: str, **detail: float) -> None:
        key = round(at / grid) * grid
        hint = raw.setdefault(key, BoundaryHint(at_sec=key))
        if kind not in hint.kinds:
            hint.kinds.append(kind)
        hint.detail.update(detail)

    for span in situations:
        if span.end_sec - span.start_sec >= hold:
            add(span.start_sec, "situation_hold", situation_sec=span.end_sec - span.start_sec)
            add(span.end_sec, "situation_hold", situation_sec=span.end_sec - span.start_sec)

    if tension.times:
        start = tension.times[0]
        run_start = start
        for t in tension.times:
            if tension.at(t) > low:
                if t - run_start >= floor_hold:
                    add(run_start, "tension_floor", floor_sec=t - run_start)
                    add(t, "tension_floor", floor_sec=t - run_start)
                run_start = t
        if tension.times[-1] - run_start >= floor_hold:
            add(run_start, "tension_floor", floor_sec=tension.times[-1] - run_start)

    for t in shift_times:
        add(t, "topic_shift")

    return sorted(
        (h for h in raw.values() if h.strength >= min_hints),
        key=lambda h: h.at_sec,
    )
