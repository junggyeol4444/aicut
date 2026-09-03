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


def _split_long(utterances, max_scene_sec: float):
    """Yield (start, end, text) no longer than the cap, splitting on words.

    An utterance can outrun the cap by itself - a recogniser running over music
    merges its guesses into one long span - and a scene that long is what
    retrieval cannot pick a moment out of. Word timings give the seams; without
    them the text goes with the first piece rather than being invented into
    parts nobody said.
    """
    if not utterances:
        return
    start = utterances[0].start_sec
    end = utterances[-1].end_sec
    text = " ".join(u.text for u in utterances)
    if end - start <= max_scene_sec:
        yield start, end, text
        return

    words = [w for u in utterances for w in (u.words or []) if w.get("start") is not None]
    if not words:
        at = start
        while at < end:
            piece_end = min(end, at + max_scene_sec)
            yield at, piece_end, text if at == start else ""
            at = piece_end
        return

    words.sort(key=lambda w: float(w["start"]))
    piece: list = []
    piece_start = start
    for word in words:
        if piece and float(word["end"] if word.get("end") is not None else word["start"]) - piece_start > max_scene_sec:
            yield piece_start, float(piece[-1].get("end") or piece[-1]["start"]), " ".join(
                str(w.get("word", "")).strip() for w in piece
            ).strip()
            piece = []
            piece_start = float(word["start"])
        piece.append(word)
    if piece:
        yield piece_start, end, " ".join(str(w.get("word", "")).strip() for w in piece).strip()


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
        profile: CalibrationProfile | None = None,
        max_gap_sec: float | None = None,
        max_scene_sec: float | None = None,
        source_sec: float | None = None,
    ) -> "SceneIndex":
        """Group speech into scenes, then tag each with the events it overlaps.

        Two judgements decide what a scene is, and neither may be a constant
        (2장, 17.1): how long a pause ends one, and how long one may run.

        The length cap is not decoration. Without it, speech that never pauses
        for longer than the gap becomes a single scene - and a six-hour source
        measured here produced exactly one, spanning the whole broadcast. Every
        beat then retrieved the entire recording, so a plan came out with 179
        cuts covering 44 days of source and 828,949 subtitle lines. Retrieval
        cannot pick a moment out of a unit that is the whole thing.

        A cap in seconds alone does not say that. On a 30 second film the 90
        second cap allowed one scene covering all of it, and the plan came out
        as six cuts of 0.00-30.00 - the same failure, too small to be caught by
        an absolute number. What has to hold is relative: a scene may not be
        the broadcast. Both bounds apply, whichever is tighter.
        """
        if profile is not None:
            if max_gap_sec is None:
                max_gap_sec = profile.get_float("retrieval.scene_gap_sec")
            if max_scene_sec is None:
                max_scene_sec = profile.get_float("retrieval.scene_max_sec")
        if max_gap_sec is None:
            max_gap_sec = 2.0
        if max_scene_sec is None:
            max_scene_sec = 90.0
        ratio = profile.get_float("retrieval.scene_max_source_ratio") if profile else 0.2
        if source_sec and ratio > 0:
            max_scene_sec = min(max_scene_sec, source_sec * ratio)
        scenes: list[Scene] = []
        current: list[Utterance] = []

        def flush() -> None:
            if not current:
                return
            speakers = {u.speaker for u in current}
            speaker = next(iter(speakers)) if len(speakers) == 1 else UNKNOWN_SPEAKER

            # The gap check only ever fires between utterances, so a single
            # utterance longer than the cap slipped through whole: on a real
            # film one ran 14.9s against a 6s cap, and the cut built from it
            # covered half the source. Cut long spans at word boundaries.
            pieces: list[list] = [[]]
            span_start = current[0].start_sec
            for utterance in current:
                if pieces[0] and utterance.end_sec - span_start > max_scene_sec:
                    pieces.append([])
                    span_start = utterance.start_sec
                pieces[-1].append(utterance)

            for piece in pieces:
                for start, end, text in _split_long(piece, max_scene_sec):
                    scenes.append(Scene(
                        start_sec=start, end_sec=end, text=text,
                        speaker=speaker, tokens=tokenize(text),
                    ))
            current.clear()

        for utterance in sorted(utterances, key=lambda u: u.start_sec):
            if current and (
                utterance.start_sec - current[-1].end_sec > max_gap_sec
                or utterance.speaker != current[-1].speaker
                or utterance.end_sec - current[0].start_sec > max_scene_sec
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
                    #
                    # Split to the same cap as speech scenes. These bypassed it
                    # entirely, so on a real film every cap was in place and a
                    # 15.9s scene still reached the plan through this branch,
                    # while the cap said 6s. A hole in one path is a hole.
                    at = mention.source_start_sec
                    while at < mention.source_end_sec:
                        piece_end = min(mention.source_end_sec, at + max_scene_sec)
                        scenes.append(Scene(
                            start_sec=at,
                            end_sec=piece_end,
                            text=mention.quote,
                            event_ids=[event.event_id],
                            roles=[mention.role],
                            tokens=tokenize(f"{mention.quote} {event.summary} {mention.role}"),
                        ))
                        at = piece_end
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
