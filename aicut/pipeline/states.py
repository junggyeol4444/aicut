"""Pipeline state machine (14장).

Two states carry the design decisions of the document:

``NO_CONTENT`` is a *normal* termination. A broadcast that contains nothing worth
making produces nothing, and that is a correct answer (1.3, 16장) - not a failure,
not an excuse to lower the bar until something comes out.

``REVIEW_PENDING`` is a mandatory gate, not an optional review step (11.3). The
zero-touch design was rejected: nothing reaches the public without a person
saying so. The gate can be automated later, by explicit choice, once the system
has earned it.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    QUEUED = "QUEUED"
    PARSING = "PARSING"                # multimodal extraction
    UNDERSTANDING = "UNDERSTANDING"    # events, people, relations
    DISCOVERING = "DISCOVERING"        # content candidates
    EVALUATING = "EVALUATING"          # worth making?
    PLANNING = "PLANNING"              # structure + retrieval + pacing
    RENDERING = "RENDERING"
    PACKAGED = "PACKAGED"
    REVIEW_PENDING = "REVIEW_PENDING"  # mandatory human gate (11.3)
    PUBLISHED = "PUBLISHED"

    NO_CONTENT = "NO_CONTENT"          # normal end: nothing worth producing
    FAILED = "FAILED"
    RETRY_QUEUED = "RETRY_QUEUED"      # e.g. upload quota spent (11.4)


_NEXT: dict[State, set[State]] = {
    State.QUEUED: {State.PARSING, State.FAILED},
    State.PARSING: {State.UNDERSTANDING, State.FAILED},
    State.UNDERSTANDING: {State.DISCOVERING, State.FAILED},
    State.DISCOVERING: {State.EVALUATING, State.NO_CONTENT, State.FAILED},
    State.EVALUATING: {State.PLANNING, State.NO_CONTENT, State.FAILED},
    State.PLANNING: {State.RENDERING, State.NO_CONTENT, State.FAILED},
    State.RENDERING: {State.PACKAGED, State.FAILED},
    State.PACKAGED: {State.REVIEW_PENDING, State.FAILED},
    State.REVIEW_PENDING: {State.PUBLISHED, State.RETRY_QUEUED, State.FAILED},
    State.RETRY_QUEUED: {State.PUBLISHED, State.RETRY_QUEUED, State.FAILED},
    State.PUBLISHED: set(),
    State.NO_CONTENT: set(),
    # A failed render may be re-run from the surviving edit plan (16장), which is
    # why FAILED leads back into the pipeline rather than being terminal.
    State.FAILED: {State.PARSING, State.UNDERSTANDING, State.DISCOVERING, State.EVALUATING,
                   State.PLANNING, State.RENDERING, State.PACKAGED},
}

TERMINAL = {State.PUBLISHED, State.NO_CONTENT}


def can_transition(current: State, target: State) -> bool:
    return target in _NEXT.get(current, set())


def next_states(current: State) -> set[State]:
    return set(_NEXT.get(current, set()))
