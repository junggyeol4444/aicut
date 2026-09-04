"""DISCOVERING: what contents exist in this broadcast (6장).

The question is not "which moments are good" but "what is in here that could
stand on its own as a video". The count is an output, not a setting - including
zero, which 1.3 and 16장 both name as a correct answer.

The split is by event, never by screen state (6.2). Mixed screens holding one
event are one content; one unchanging screen holding several events is several
contents. That rule is enforced here by handing the producer the event graph as
the unit of division, and by checking afterwards that a candidate is anchored in
events at all.
"""

from __future__ import annotations

import logging

from aicut.models import ContentCandidate, Decision, Event
from aicut.pipeline.context import RunContext

log = logging.getLogger(__name__)


def run(ctx: RunContext) -> list[ContentCandidate]:
    events = ctx.store.events(ctx.project.project_id)
    if not events:
        ctx.note("discovery_note", "no events were built; nothing to discover")
        ctx.store.replace_candidates(ctx.project.project_id, [])
        return []

    windows = ctx.store.windows(ctx.project.project_id)
    raw = ctx.producer.discover_candidates({
        "broadcast": {
            "duration_sec": ctx.project.duration_sec,
            "length_hint_sec": ctx.project.length_hint_sec,
        },
        "events": [_event_payload(e) for e in events],
        "windows": [
            {"start_sec": w.start_sec, "end_sec": w.end_sec, "summary": w.summary, "markers": w.markers}
            for w in windows if w.notable
        ],
        "reference_span_sec": ctx.project.duration_sec,
        "boundary_hints": ctx.report.get("boundary_hints", []),
    })

    known_events = {e.event_id for e in events}
    candidates: list[ContentCandidate] = []
    for item in raw:
        related = [eid for eid in (item.get("related_event_ids") or []) if eid in known_events]
        if not related:
            # 6.2: a content that is not anchored in an event has no boundary we
            # can defend, so it is dropped rather than produced.
            log.info("dropping a candidate with no event anchor: %r", item.get("core_summary", "")[:60])
            continue
        candidates.append(ContentCandidate(
            project_id=ctx.project.project_id,
            core_summary=item.get("core_summary", ""),
            related_event_ids=related,
            required_context=item.get("required_context", ""),
            required_context_sec=float(item.get("required_context_sec", 0.0)),
            independence_score=float(item.get("independence_score", 0.0)),
            density_score=float(item.get("density_score", 0.0)),
            has_resolution=bool(item.get("has_resolution", True)),
            decision=Decision.HOLD,
            decision_reason=item.get("reason", ""),
        ))

    ctx.store.replace_candidates(ctx.project.project_id, candidates)
    ctx.note("candidates_found", len(candidates))
    return candidates


def _event_payload(event: Event) -> dict:
    start, end = event.span()
    return {
        "event_id": event.event_id,
        "summary": event.summary,
        "people": event.people,
        "relations": event.relations,
        "span_sec": [start, end],
        "mentions": [
            {
                "source_start_sec": m.source_start_sec,
                "source_end_sec": m.source_end_sec,
                "role": m.role,
                "quote": m.quote,
            }
            for m in event.mentions
        ],
    }
