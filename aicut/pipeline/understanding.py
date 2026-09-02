"""UNDERSTANDING: two passes over the whole broadcast, then a memory (5장).

An editor watches the entire recording, fast, and slows down where something is
there. That is the shape of this stage, and it is deliberately *not* shot-change
detection: 5.1 is explicit that a screen which does not change for thirty minutes
is not thirty minutes in which nothing happened, so the first pass covers every
second of the source with no skipping.

The first pass reads each window with the accumulated memory of everything before
it, which is what lets a remark at 03:41 be recognised as being about the thing
that happened at 00:32 (5.4). Without that carry-over the passes would be
independent fragments and non-linear reconstruction (2.4) would have nothing to
work with.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aicut.analysis.signals import boundary_hints, label_situations, topic_shifts
from aicut.media import faces as faces_mod
from aicut.media import vision as vision_mod
from aicut.models import DetailSpan, Event, EventMention, SituationLabel, WindowSummary
from aicut.pipeline.context import RunContext

log = logging.getLogger(__name__)

MEMORY_WINDOWS = 6          # how many earlier window summaries travel with the pass


def run(ctx: RunContext, *, sample_frames: bool = False) -> RunContext:
    duration = ctx.project.duration_sec
    utterances = ctx.store.utterances(ctx.project.project_id)

    frames = _sample_frames(ctx, duration) if sample_frames else []
    _read_faces(ctx, frames)

    situations = label_situations(
        duration, utterances, ctx.signals.motion, ctx.profile,
        face_ratio=faces_mod.face_ratio_lookup(ctx.signals.faces) if ctx.signals.faces else None,
    )
    ctx.store.replace_situations(ctx.project.project_id, situations)

    windows = _first_pass(ctx, duration, situations, frames=frames)
    ctx.store.replace_windows(ctx.project.project_id, windows)

    details = _second_pass(ctx, windows, sample_frames=sample_frames)
    ctx.store.replace_details(ctx.project.project_id, details)

    events = _build_events(ctx, windows, details)
    ctx.store.replace_events(ctx.project.project_id, events)

    shifts = topic_shifts(utterances, ctx.profile)
    hints = boundary_hints(situations, ctx.signals.tension, shifts, ctx.profile)

    ctx.note("first_pass_windows", len(windows))
    ctx.note("second_pass_windows", len(details))
    ctx.note("events", len(events))
    ctx.note("boundary_hints", [{"at_sec": h.at_sec, "kinds": h.kinds} for h in hints])
    ctx.note("situation_mix", _situation_mix(situations, duration))
    if not ctx.signals.faces:
        ctx.note(
            "vision_note",
            "no face signal: talk/gameplay labelling stays UNKNOWN rather than being guessed (5.3)",
        )
    return ctx


def _sample_frames(ctx: RunContext, duration: float) -> list[tuple[float, str]]:
    """Sample the whole broadcast once, at the first pass's density.

    Sampled once and reused by the face reader, the situation labeller and the
    first pass; decoding six hours three times over would be the obvious way to
    make R3 (processing time) worse for no gain.
    """
    interval = ctx.profile.get_float("scan.pass1_frame_interval_sec")
    chunk = ctx.profile.get_float("scan.long_source_chunk_sec")
    out_dir = ctx.project_dir / "frames" / "pass1"
    frames: list[tuple[float, str]] = []
    at = 0.0
    while at < duration:
        span = min(chunk, duration - at)
        samples = vision_mod.sample_frames(
            ctx.project.file_path, out_dir, start_sec=at, duration_sec=span,
            interval_sec=interval, prefix=f"w{int(at)}",
        )
        frames.extend((f.at_sec, f.path) for f in samples)
        at += span
    return frames


def _read_faces(ctx: RunContext, frames: list[tuple[float, str]]) -> None:
    if not frames:
        return
    detector = faces_mod.build_detector()
    if detector is None:
        return
    ctx.signals.faces = detector.read_frames(frames)
    ctx.signals.save(ctx.signal_cache_path)


# ---------------------------------------------------------------------------
def _first_pass(
    ctx: RunContext, duration: float, situations, *, frames: list[tuple[float, str]] | None = None
) -> list[WindowSummary]:
    """Cover the whole source, window by window, carrying memory forward (5.1)."""
    window_sec = ctx.profile.get_float("scan.pass1_window_sec")
    trigger = ctx.profile.get("scan.pass2_trigger")

    # The scan density is a provisional guess (17.5) tuned for a broadcast, and
    # on a short clip it degenerates: one window means one summary, which means
    # an event with a single mention, which is below the discovery floor - so
    # the run ends NO_CONTENT and the reason does not mention the window size
    # that caused it. Say it here, where both numbers are in hand.
    if duration < 2 * window_sec:
        ctx.report.setdefault("scan_density", []).append({
            "source_sec": round(duration, 1),
            "pass1_window_sec": window_sec,
            "windows": max(1, int(duration // window_sec) + (1 if duration % window_sec else 0)),
            "detail": (
                f"this source is {duration:.0f}s and the first pass reads it in {window_sec:.0f}s "
                f"windows, so it gets very few. An event needs "
                f"{ctx.profile.get('discovery.min_event_mentions')} mentions to be considered, and a "
                f"mention comes from a window - a short source can therefore produce nothing at all. "
                f"Lower scan.pass1_window_sec for clips this size (17.1: it is a profile value, "
                f"not a constant)."
            ),
        })

    utterances = ctx.store.utterances(ctx.project.project_id)
    sampled = sorted(frames or [])
    summaries: list[WindowSummary] = []

    at = 0.0
    while at < duration:
        end = min(duration, at + window_sec)
        window_utterances = [u for u in utterances if u.end_sec > at and u.start_sec < end]
        situation = _situation_at(situations, at, end)
        window_frames = [path for sec, path in sampled if at <= sec < end]

        payload = {
            "window": {"start_sec": at, "end_sec": end},
            "utterances": [
                {"start_sec": u.start_sec, "end_sec": u.end_sec, "speaker": u.speaker, "text": u.text}
                for u in window_utterances
            ],
            "situation": situation,
            "tension_peak": ctx.signals.tension.peak(at, end),
            "tension_mean": ctx.signals.tension.mean(at, end),
            "signal_markers": _markers(ctx, at, end),
            "frames": window_frames,
            "face_ratio": _mean_face_ratio(ctx, at, end),
            "pass2_trigger": trigger,
            "memory": _memory(summaries),
        }
        answer = ctx.producer.summarize_window(payload)
        summaries.append(WindowSummary(
            start_sec=at,
            end_sec=end,
            summary=answer.get("summary", ""),
            people=answer.get("people", []) or [],
            topics=answer.get("topics", []) or [],
            screen=answer.get("screen", situation),
            notable=bool(answer.get("notable", False)),
            notable_reason=answer.get("notable_reason", ""),
            tension_peak=payload["tension_peak"],
            markers=answer.get("markers", []) or [],
        ))
        at = end
    return summaries


def _second_pass(ctx: RunContext, windows: list[WindowSummary], *, sample_frames: bool) -> list[DetailSpan]:
    """Go back over the marked windows, finely (5.1)."""
    frame_interval = ctx.profile.get_float("scan.pass2_frame_interval_sec")
    utterances = ctx.store.utterances(ctx.project.project_id)
    frames_dir = ctx.project_dir / "frames" / "pass2"
    details: list[DetailSpan] = []

    for window in windows:
        if not window.notable:
            continue
        inside = [u for u in utterances if u.end_sec > window.start_sec and u.start_sec < window.end_sec]
        frames = []
        if sample_frames:
            frames = [
                f.path for f in vision_mod.sample_frames(
                    ctx.project.file_path, frames_dir, start_sec=window.start_sec,
                    duration_sec=window.end_sec - window.start_sec, interval_sec=frame_interval,
                    prefix=f"d{int(window.start_sec)}",
                )
            ]
        answer = ctx.producer.detail_window({
            "window": {"start_sec": window.start_sec, "end_sec": window.end_sec},
            "why_marked": window.notable_reason,
            "summary": window.summary,
            "utterances": [
                {"start_sec": u.start_sec, "end_sec": u.end_sec, "speaker": u.speaker, "text": u.text,
                 "words": u.words}
                for u in inside
            ],
            "silences": [
                {"start_sec": s.start_sec, "end_sec": s.end_sec}
                for s in ctx.signals.silences
                if s.end_sec > window.start_sec and s.start_sec < window.end_sec
            ],
            "frames": frames,
        })
        details.append(DetailSpan(
            start_sec=window.start_sec,
            end_sec=window.end_sec,
            exact_start_sec=answer.get("exact_start_sec"),
            exact_end_sec=answer.get("exact_end_sec"),
            beats=answer.get("beats", []) or [],
            notes=answer.get("notes", ""),
        ))
    return details


def _build_events(ctx: RunContext, windows: list[WindowSummary], details: list[DetailSpan]) -> list[Event]:
    """Fold both passes into the event graph that is the long-term memory (5.4).

    A source past the profile's split point is folded in chunks and the chunks
    are then merged (16장): handing ten hours of window summaries to one call
    would blow the context, and an event that starts in hour 1 and pays off in
    hour 9 must still come out as one event - which is what the merge pass is
    for.
    """
    duration = ctx.project.duration_sec
    split = ctx.profile.get_float("scan.long_source_split_sec")
    if duration <= split or not windows:
        return _events_from(ctx, windows, details)

    chunk_sec = ctx.profile.get_float("scan.long_source_chunk_sec")
    log.info(
        "source is %.1fh, past the %.1fh split point: folding the event graph in %.1fh chunks (16장)",
        duration / 3600, split / 3600, chunk_sec / 3600,
    )
    partial: list[Event] = []
    at = 0.0
    while at < duration:
        end = at + chunk_sec
        chunk_windows = [w for w in windows if at <= w.start_sec < end]
        chunk_details = [d for d in details if at <= d.start_sec < end]
        if chunk_windows:
            partial.extend(_events_from(ctx, chunk_windows, chunk_details))
        at = end
    return _merge_events(ctx, partial)


def _events_from(ctx: RunContext, windows: list[WindowSummary], details: list[DetailSpan]) -> list[Event]:
    raw = ctx.producer.build_events({
        "windows": [
            {
                "start_sec": w.start_sec, "end_sec": w.end_sec, "summary": w.summary, "people": w.people,
                "topics": w.topics, "screen": w.screen, "notable": w.notable, "markers": w.markers,
            }
            for w in windows
        ],
        "details": [
            {"start_sec": d.start_sec, "end_sec": d.end_sec, "beats": d.beats, "notes": d.notes}
            for d in details
        ],
    })

    events = [
        Event(
            project_id=ctx.project.project_id,
            summary=item.get("summary", ""),
            people=item.get("people", []) or [],
        )
        for item in raw
    ]
    for event, item in zip(events, raw):
        event.mentions = [
            EventMention(
                event_id=event.event_id,
                source_start_sec=float(m.get("source_start_sec", 0.0)),
                source_end_sec=float(m.get("source_end_sec", 0.0)),
                role=m.get("role", ""),
                quote=m.get("quote", ""),
            )
            for m in item.get("mentions", []) or []
        ]
        # Relations arrive as indices into this same list; resolve them to ids so
        # the graph survives a round trip through the database.
        event.relations = [
            {"event_id": events[int(rel["event_index"])].event_id, "kind": rel.get("kind", "related")}
            for rel in item.get("relations", []) or []
            if isinstance(rel.get("event_index"), int) and 0 <= int(rel["event_index"]) < len(events)
        ]
    return [e for e in events if e.mentions]


def _merge_events(ctx: RunContext, partial: list[Event]) -> list[Event]:
    """Unify the per-chunk events, carrying every mention into the merged event."""
    if len(partial) < 2:
        return partial

    groups = ctx.producer.merge_events({
        "events": [
            {
                "index": i,
                "summary": event.summary,
                "people": event.people,
                "span_sec": list(event.span()),
                "mentions": [
                    {"source_start_sec": m.source_start_sec, "source_end_sec": m.source_end_sec,
                     "role": m.role, "quote": m.quote}
                    for m in event.mentions
                ],
            }
            for i, event in enumerate(partial)
        ]
    })

    merged: list[Event] = []
    claimed: set[int] = set()
    for group in groups:
        indices = [int(i) for i in (group.get("member_indices") or []) if 0 <= int(i) < len(partial)]
        indices = [i for i in indices if i not in claimed]
        if not indices:
            continue
        claimed.update(indices)
        members = [partial[i] for i in indices]
        event = Event(
            project_id=ctx.project.project_id,
            summary=group.get("summary") or members[0].summary,
            people=group.get("people") or sorted({p for m in members for p in m.people}),
        )
        for member in members:
            for mention in member.mentions:
                event.mentions.append(EventMention(
                    event_id=event.event_id,
                    source_start_sec=mention.source_start_sec,
                    source_end_sec=mention.source_end_sec,
                    role=mention.role,
                    quote=mention.quote,
                ))
        event.mentions.sort(key=lambda m: m.source_start_sec)
        merged.append(event)

    # An event the merge pass forgot is kept as itself rather than dropped: a
    # chunking artefact must never lose material from the broadcast.
    for i, event in enumerate(partial):
        if i not in claimed:
            merged.append(event)

    for group, event in zip(groups, merged):
        event.relations = [
            {"event_id": merged[int(rel["event_index"])].event_id, "kind": rel.get("kind", "related")}
            for rel in (group.get("relations") or [])
            if isinstance(rel.get("event_index"), int) and 0 <= int(rel["event_index"]) < len(merged)
        ]
    return merged


# ---------------------------------------------------------------------------
def _memory(summaries: list[WindowSummary]) -> dict:
    """What the pass knows so far - recent detail plus everything's people/topics."""
    recent = summaries[-MEMORY_WINDOWS:]
    people, topics = set(), set()
    for w in summaries:
        people.update(w.people)
        topics.update(w.topics)
    return {
        "recent_windows": [
            {"start_sec": w.start_sec, "end_sec": w.end_sec, "summary": w.summary, "markers": w.markers}
            for w in recent
        ],
        "known_people": sorted(people),
        "known_topics": sorted(topics)[:60],
        "open_threads": [w.notable_reason for w in summaries if w.notable][-MEMORY_WINDOWS:],
    }


def _markers(ctx: RunContext, start: float, end: float) -> list[str]:
    marks: list[str] = []
    high = ctx.profile.get_float("tension.high")
    low = ctx.profile.get_float("tension.low")
    peak = ctx.signals.tension.peak(start, end)
    if peak >= high:
        marks.append("tension_peak")
    if ctx.signals.tension.held_low_for(start, end, low):
        marks.append("tension_floor")
    long_silence = [s for s in ctx.signals.silences if s.start_sec >= start and s.end_sec <= end
                    and s.duration >= ctx.profile.get_float("pacing.cut_min_sec")]
    if long_silence:
        marks.append("long_silence")
    if any(b.end_sec > start and b.start_sec < end for b in ctx.signals.bursts):
        marks.append("vocal_burst")
    return marks


def _mean_face_ratio(ctx: RunContext, start: float, end: float) -> float | None:
    inside = [f.face_ratio for f in ctx.signals.faces if start <= f.at_sec < end]
    return round(sum(inside) / len(inside), 4) if inside else None


def _situation_at(situations, start: float, end: float) -> str:
    overlapping = [s for s in situations if s.end_sec > start and s.start_sec < end]
    if not overlapping:
        return SituationLabel.UNKNOWN.value
    dominant = max(overlapping, key=lambda s: min(end, s.end_sec) - max(start, s.start_sec))
    return dominant.label.value


def _situation_mix(situations, duration: float) -> dict[str, float]:
    if duration <= 0:
        return {}
    mix: dict[str, float] = {}
    for span in situations:
        mix[span.label.value] = mix.get(span.label.value, 0.0) + (span.end_sec - span.start_sec)
    return {k: round(v / duration, 3) for k, v in sorted(mix.items())}
