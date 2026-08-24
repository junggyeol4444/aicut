"""Data model (13장).

The one structural decision that matters here: **an episode is not a time range.**
Storing an episode as ``(start_sec, end_sec)`` over the source would make 2.4
(non-linear reconstruction) and 5.4 (linking moments that sit hours apart)
impossible to express. So an episode owns an ordered set of cuts, and each cut
carries its own source position; the order in the finished video is
``sequence_order``, which has nothing to do with source time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


def new_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------
class PacingMode(str, Enum):
    """9.3 - what to do with the stillness inside a cut."""

    KEEP = "KEEP"    # comedic beat: preserve it exactly
    TRIM = "TRIM"    # compress it, leave some breath
    CUT = "CUT"      # dead air: remove the span


class Decision(str, Enum):
    """6.3 - the verdict on a discovered content candidate."""

    PRODUCE = "produce"
    COMBINE = "combine"
    HOLD = "hold"
    REJECT = "reject"


class SituationLabel(str, Enum):
    """5.3 - a screen state. An internal analysis signal, never an output category."""

    SOLO_TALK = "solo_talk"
    GAMEPLAY = "gameplay"
    MULTI_PERSON = "multi_person"
    AWAY = "away"
    UNKNOWN = "unknown"


UNKNOWN_SPEAKER = "UNKNOWN"


# --------------------------------------------------------------------------
# source understanding (5장)
# --------------------------------------------------------------------------
@dataclass
class Utterance:
    """One spoken unit with word-level timing, tagged with its audio track."""

    start_sec: float
    end_sec: float
    text: str
    speaker: str = UNKNOWN_SPEAKER
    track: str = "mic"
    words: list[dict[str, Any]] = field(default_factory=list)
    confidence: float | None = None


@dataclass
class SituationSpan:
    """A stretch of broadcast that shares one screen state (5.3)."""

    start_sec: float
    end_sec: float
    label: SituationLabel = SituationLabel.UNKNOWN
    speakers: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowSummary:
    """Output of one first-pass window (5.1). The whole broadcast is covered by these."""

    start_sec: float
    end_sec: float
    summary: str
    people: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    screen: str = ""
    notable: bool = False
    notable_reason: str = ""
    tension_peak: float = 0.0
    markers: list[str] = field(default_factory=list)


@dataclass
class DetailSpan:
    """Output of one second-pass sweep over a marked window (5.1)."""

    start_sec: float
    end_sec: float
    beats: list[dict[str, Any]] = field(default_factory=list)
    exact_start_sec: float | None = None
    exact_end_sec: float | None = None
    notes: str = ""


@dataclass
class EventMention:
    """One of the scattered moments belonging to a single event (TB_EVENT_MENTION)."""

    event_id: str
    source_start_sec: float
    source_end_sec: float
    role: str = ""          # first_mention / related_talk / callback / conflict / result ...
    quote: str = ""
    mention_id: int | None = None


@dataclass
class Event:
    """A thing that happened in the broadcast, with all of its moments (TB_EVENT, 5.4)."""

    event_id: str = field(default_factory=new_id)
    project_id: str = ""
    summary: str = ""
    people: list[str] = field(default_factory=list)
    relations: list[dict[str, str]] = field(default_factory=list)   # {"event_id":..., "kind":"causes"}
    mentions: list[EventMention] = field(default_factory=list)

    def span(self) -> tuple[float, float]:
        """First and last source second this event touches - a range, not a cut list."""
        if not self.mentions:
            return (0.0, 0.0)
        return (
            min(m.source_start_sec for m in self.mentions),
            max(m.source_end_sec for m in self.mentions),
        )


# --------------------------------------------------------------------------
# discovery / evaluation (6장)
# --------------------------------------------------------------------------
@dataclass
class ContentCandidate:
    """Something the system believes could stand alone as a video (TB_CONTENT_CANDIDATE)."""

    candidate_id: str = field(default_factory=new_id)
    project_id: str = ""
    core_summary: str = ""
    related_event_ids: list[str] = field(default_factory=list)
    required_context: str = ""
    required_context_sec: float = 0.0
    independence_score: float = 0.0
    density_score: float = 0.0
    has_resolution: bool = True
    decision: Decision = Decision.HOLD
    decision_reason: str = ""
    combine_with: list[str] = field(default_factory=list)
    human_verdict: str | None = None       # 15.4 agree / disagree, feeds 12.3 B


# --------------------------------------------------------------------------
# planning (7-9장)
# --------------------------------------------------------------------------
@dataclass
class Cut:
    """One cut on the finished timeline (TB_EDIT_TIMELINE).

    ``sequence_order`` is the position in the output; ``source_start_sec`` is
    where it came from. The two are deliberately unrelated (2.4).
    """

    sequence_order: int
    source_start_sec: float
    source_end_sec: float
    speaker_tag: str = UNKNOWN_SPEAKER
    scene_role: str = ""                    # background / core / result ... (8.2)
    pacing_mode: PacingMode = PacingMode.TRIM
    pacing_reason: str = ""
    visual_effect: dict[str, Any] = field(default_factory=dict)
    audio_effect: dict[str, Any] = field(default_factory=dict)
    subtitle_ref: str | None = None
    silences: list[dict[str, float]] = field(default_factory=list)
    remove_spans: list[list[float]] = field(default_factory=list)
    """Source spans the renderer must drop from inside this cut (9.3 TRIM/CUT)."""
    cut_id: int | None = None

    def kept_spans(self) -> list[tuple[float, float]]:
        """The parts of this cut that survive pacing, in source time."""
        spans = [(self.source_start_sec, self.source_end_sec)]
        for start, end in sorted(tuple(r) for r in self.remove_spans):
            out: list[tuple[float, float]] = []
            for a, b in spans:
                if end <= a or start >= b:
                    out.append((a, b))
                    continue
                if start > a:
                    out.append((a, min(start, b)))
                if end < b:
                    out.append((max(end, a), b))
            spans = out
        return [(a, b) for a, b in spans if b - a > 1e-3]

    @property
    def source_duration(self) -> float:
        return max(0.0, self.source_end_sec - self.source_start_sec)


@dataclass
class SubtitleLine:
    start_sec: float
    end_sec: float
    text: str
    speaker: str = UNKNOWN_SPEAKER
    emphasis: bool = False
    style: str | None = None


@dataclass
class Episode:
    """A video that was decided into existence (TB_EPISODE). No start/end columns."""

    episode_id: str = field(default_factory=new_id)
    project_id: str = ""
    candidate_ids: list[str] = field(default_factory=list)
    title_candidates: list[str] = field(default_factory=list)
    planned_structure: dict[str, Any] = field(default_factory=dict)   # decided per content (7장)
    target_type: str = ""                    # AI's own word for the form, not a fixed enum
    planned_duration_sec: float = 0.0
    timeline: list[Cut] = field(default_factory=list)
    subtitles: list[SubtitleLine] = field(default_factory=list)
    output_mp4_path: str | None = None
    thumbnail_path: str | None = None
    thumbnail_candidates: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    render_status: str = "pending"
    review_status: str = "not_submitted"
    notes: str = ""


@dataclass
class Project:
    """One source broadcast being processed (TB_PROJECT)."""

    project_id: str = field(default_factory=new_id)
    file_path: str = ""
    duration_sec: float = 0.0
    status: str = "QUEUED"
    profile_name: str = "default"
    created_at: str = ""
    length_hint_sec: float | None = None     # 2.6: a hint, never a constraint
    channel_ref: str = ""


def to_dict(obj: Any) -> Any:
    """dataclass -> JSON-ready dict, with enums flattened to their values."""
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj
