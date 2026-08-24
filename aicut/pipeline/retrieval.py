"""Scene retrieval (8.1).

The planner asks for scenes in words - "where the event was first mentioned",
"where the other person first reacted", "where the result happened" - and this
module finds candidates anywhere in the source for the producer to verify. It
searches the whole broadcast, not a neighbourhood of the candidate, because a
video is allowed to reach hours away for the shot it needs (2.4).

Scoring is lexical by default and takes an embedder when one is available; either
way the retrieved set is a shortlist, and the choice among them is a judgement
made by the producer (8.1: retrieve N, then verify).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Sequence

from aicut.config import CalibrationProfile
from aicut.models import DetailSpan, Event, UNKNOWN_SPEAKER, Utterance

_WORD = re.compile(r"[\w']+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


@dataclass
class Scene:
    """A retrievable unit of source: a run of speech with its context."""

    start_sec: float
    end_sec: float
    text: str
    speaker: str = UNKNOWN_SPEAKER
    event_ids: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass
class ScoredScene:
    scene: Scene
    score: float
    matched: list[str] = field(default_factory=list)


class SceneIndex:
    """A BM25-style index over the broadcast's scenes."""

    K1 = 1.4
    B = 0.75

    def __init__(self, scenes: Sequence[Scene]):
        self.scenes = list(scenes)
        self._df: Counter[str] = Counter()
        for scene in self.scenes:
            self._df.update(set(scene.tokens))
        lengths = [len(s.tokens) for s in self.scenes] or [1]
        self._avg_len = sum(lengths) / len(lengths)

    @classmethod
    def build(
        cls,
        utterances: Sequence[Utterance],
        events: Sequence[Event],
        details: Sequence[DetailSpan] = (),
        *,
        max_gap_sec: float = 2.0,
    ) -> "SceneIndex":
        """Group speech into scenes, then tag each with the events it overlaps."""
        scenes: list[Scene] = []
        current: list[Utterance] = []

        def flush() -> None:
            if not current:
                return
            speakers = {u.speaker for u in current}
            text = " ".join(u.text for u in current)
            scenes.append(Scene(
                start_sec=current[0].start_sec,
                end_sec=current[-1].end_sec,
                text=text,
                speaker=next(iter(speakers)) if len(speakers) == 1 else UNKNOWN_SPEAKER,
                tokens=tokenize(text),
            ))
            current.clear()

        for utterance in sorted(utterances, key=lambda u: u.start_sec):
            if current and (
                utterance.start_sec - current[-1].end_sec > max_gap_sec
                or utterance.speaker != current[-1].speaker
            ):
                flush()
            current.append(utterance)
        flush()

        for event in events:
            for mention in event.mentions:
                overlapping = [
                    s for s in scenes
                    if s.end_sec > mention.source_start_sec and s.start_sec < mention.source_end_sec
                ]
                if not overlapping:
                    # A mention with no speech under it is still retrievable -
                    # 16장 requires the system to keep working through silence.
                    scene = Scene(
                        start_sec=mention.source_start_sec,
                        end_sec=mention.source_end_sec,
                        text=mention.quote,
                        event_ids=[event.event_id],
                        roles=[mention.role],
                        tokens=tokenize(f"{mention.quote} {event.summary} {mention.role}"),
                    )
                    scenes.append(scene)
                    continue
                for scene in overlapping:
                    if event.event_id not in scene.event_ids:
                        scene.event_ids.append(event.event_id)
                    if mention.role and mention.role not in scene.roles:
                        scene.roles.append(mention.role)
                    scene.tokens.extend(tokenize(f"{event.summary} {mention.role}"))

        for detail in details:
            for beat in detail.beats:
                for scene in scenes:
                    at = float(beat.get("at_sec", -1))
                    if scene.start_sec <= at <= scene.end_sec:
                        scene.tokens.extend(tokenize(str(beat.get("what", ""))))

        scenes.sort(key=lambda s: s.start_sec)
        return cls(scenes)

    # ---- search ------------------------------------------------------------
    def search(
        self,
        query: str,
        profile: CalibrationProfile,
        *,
        event_id: str | None = None,
        role: str | None = None,
        near_sec: float | None = None,
        embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None,
    ) -> list[ScoredScene]:
        limit = profile.get_int("retrieval.candidates_per_query")
        floor = profile.get_float("retrieval.min_query_score")
        terms = tokenize(query)

        results: list[ScoredScene] = []
        for scene in self.scenes:
            score = self._bm25(terms, scene)
            matched = [t for t in set(terms) if t in scene.tokens]
            if event_id and event_id in scene.event_ids:
                score += 1.0
            if role and role in scene.roles:
                score += 0.5
            if near_sec is not None:
                # Mild locality preference: hours-away material still competes,
                # it just needs to earn the distance.
                distance = abs(scene.start_sec - near_sec)
                score += 0.5 * math.exp(-distance / 600.0)
            if score >= floor:
                results.append(ScoredScene(scene=scene, score=score, matched=matched))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _bm25(self, terms: Sequence[str], scene: Scene) -> float:
        if not terms or not scene.tokens:
            return 0.0
        counts = Counter(scene.tokens)
        length = len(scene.tokens)
        total = len(self.scenes) or 1
        score = 0.0
        for term in terms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            df = self._df.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            denom = freq + self.K1 * (1 - self.B + self.B * length / max(self._avg_len, 1e-6))
            score += idf * (freq * (self.K1 + 1)) / denom
        return score

    def by_event(self, event_id: str) -> list[Scene]:
        return [s for s in self.scenes if event_id in s.event_ids]
