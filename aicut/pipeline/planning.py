"""PLANNING: structure, scene retrieval, pacing, and the edit plan (7-9장).

The order of operations mirrors the document: decide how *this* content should be
shown (7장), search the source for the scenes that shape needs (8.1), lay them out
on a timeline with an editing intent per cut (8.2), then decide what happens to
the silence inside each cut (9장). The stage ends by writing an edit plan and
touching no video at all - which is what makes MVP 5 a shippable milestone whose
success test is "can a person read the plan and predict the video".

Two rules from 2장 are enforced here rather than trusted:

* the structure is whatever the producer chose for this content, and cut order
  follows that structure, not source time;
* the user's length hint is a hint. When the plan departs from it, the departure
  and its reason go into the report (2.6).
"""

from __future__ import annotations

import logging
from typing import Sequence

from aicut.analysis.pacing import PacingJudge, build_silence_contexts, trim_target
from aicut.models import (
    ContentCandidate,
    Cut,
    Episode,
    Event,
    PacingMode,
    SubtitleLine,
    UNKNOWN_SPEAKER,
    Utterance,
)
from aicut.pipeline.context import RunContext
from aicut.pipeline.retrieval import SceneIndex
from aicut.render.editplan import EditPlan
from aicut.render.ffmpeg import RenderSettings
from aicut.render.timeline import Timeline

log = logging.getLogger(__name__)


def run(
    ctx: RunContext,
    groups: Sequence[Sequence[ContentCandidate]],
    *,
    knowledge: dict | None = None,
) -> list[Episode]:
    """Plan one episode per candidate group."""
    events = {e.event_id: e for e in ctx.store.events(ctx.project.project_id)}
    utterances = ctx.store.utterances(ctx.project.project_id)
    index = SceneIndex.build(
        utterances, list(events.values()), ctx.store.details(ctx.project.project_id),
        profile=ctx.profile,
    )

    episodes: list[Episode] = []
    for group in groups:
        episode = plan_episode(ctx, list(group), events, index, utterances, knowledge=knowledge)
        if episode is not None:
            episodes.append(episode)
    ctx.note("episodes_planned", len(episodes))
    return episodes


def plan_episode(
    ctx: RunContext,
    group: list[ContentCandidate],
    events: dict[str, Event],
    index: SceneIndex,
    utterances: list[Utterance],
    *,
    knowledge: dict | None = None,
) -> Episode | None:
    group_events = [events[eid] for c in group for eid in c.related_event_ids if eid in events]
    if not group_events:
        log.warning("candidate group has no resolvable events; skipping")
        return None

    structure = ctx.producer.plan_structure({
        "content": {
            "core_summary": " / ".join(c.core_summary for c in group),
            "required_context": [c.required_context for c in group if c.required_context],
            "combined_from": [c.candidate_id for c in group],
        },
        "events": [
            {"event_id": e.event_id, "summary": e.summary, "people": e.people, "relations": e.relations}
            for e in group_events
        ],
        "mentions": [
            {
                "event_id": e.event_id,
                "source_start_sec": m.source_start_sec,
                "source_end_sec": m.source_end_sec,
                "role": m.role,
                "quote": m.quote,
            }
            for e in group_events for m in e.mentions
        ],
        "length_hint_sec": ctx.project.length_hint_sec,
        "youtube_knowledge": knowledge or {},
        "speaker_reliability": ctx.signals.speaker_reliability,
    })

    episode = Episode(
        project_id=ctx.project.project_id,
        candidate_ids=[c.candidate_id for c in group],
        planned_structure=structure,
        target_type=structure.get("target_type", ""),
    )
    episode.timeline = _lay_out_cuts(ctx, structure, index)
    if not episode.timeline:
        log.warning("no scene survived retrieval for episode %s; not producing it", episode.episode_id)
        return None

    _apply_pacing(ctx, episode)
    timeline = Timeline.from_cuts(episode.timeline)
    episode.subtitles = _subtitles(episode, timeline, utterances, ctx.signals.speaker_reliability)
    episode.planned_duration_sec = timeline.duration
    _note_length_deviation(ctx, episode, structure)
    _note_implausible_plan(ctx, episode)

    ctx.store.save_episode(episode)
    _write_plan(ctx, episode)
    return episode


# ---------------------------------------------------------------------------
def _lay_out_cuts(ctx: RunContext, structure: dict, index: SceneIndex) -> list[Cut]:
    """Turn the planned beats into cuts, in the structure's order (2.4, 8.2)."""
    head_pad = ctx.profile.get_float("retrieval.cut_padding_head_sec")
    tail_pad = ctx.profile.get_float("retrieval.cut_padding_tail_sec")
    duration = ctx.project.duration_sec

    cuts: list[Cut] = []
    for order, beat in enumerate(structure.get("beats", []) or []):
        query = beat.get("query", "") or beat.get("intent", "")
        results = index.search(
            query,
            ctx.profile,
            event_id=beat.get("must_include_event_id"),
            role=beat.get("role"),
            near_sec=beat.get("hint_start_sec"),
        )
        if not results:
            log.info("beat %r retrieved nothing", beat.get("role", order))
            continue

        chosen = ctx.producer.select_scene({
            "beat": beat,
            "candidates": [
                {
                    "index": i,
                    "start_sec": r.scene.start_sec,
                    "end_sec": r.scene.end_sec,
                    "speaker": r.scene.speaker,
                    "text": r.scene.text[:400],
                    "roles": r.scene.roles,
                    "score": round(r.score, 3),
                    "matched": r.matched,
                }
                for i, r in enumerate(results)
            ],
        })
        picked = chosen.get("chosen_index")
        if picked is None or not (0 <= int(picked) < len(results)):
            log.info("beat %r rejected every retrieved scene", beat.get("role", order))
            continue

        scene = results[int(picked)].scene
        start = float(chosen.get("start_sec", scene.start_sec)) - head_pad
        end = float(chosen.get("end_sec", scene.end_sec)) + tail_pad
        start = max(0.0, start)
        end = min(duration, end) if duration else end
        if end <= start:
            continue

        cuts.append(Cut(
            sequence_order=len(cuts),
            source_start_sec=start,
            source_end_sec=end,
            speaker_tag=chosen.get("speaker", scene.speaker) or UNKNOWN_SPEAKER,
            scene_role=beat.get("role", ""),
            visual_effect=beat.get("visual_effect", {}) or {},
            audio_effect=beat.get("audio_effect", {}) or {},
            subtitle_ref=None,
        ))
    return cuts


def _apply_pacing(ctx: RunContext, episode: Episode) -> None:
    """Decide, per silence, whether it is a beat or dead air (9장)."""
    judge = PacingJudge(ctx.profile, ctx.producer)
    utterances = ctx.store.utterances(ctx.project.project_id)

    for cut in episode.timeline:
        inside = [
            s for s in ctx.signals.silences
            if s.start_sec >= cut.source_start_sec and s.end_sec <= cut.source_end_sec
        ]
        if not inside:
            cut.pacing_mode = PacingMode.KEEP
            cut.pacing_reason = "no silence inside this cut"
            continue

        contexts = build_silence_contexts(
            inside, utterances, ctx.signals.tension, ctx.signals.motion, ctx.profile,
            scene_role=cut.scene_role,
        )
        decisions = judge.judge_all(contexts)
        removals: list[list[float]] = []
        records: list[dict] = []
        for decision in decisions:
            span = trim_target(decision, ctx.profile)
            if span and span[1] > span[0]:
                removals.append([span[0], span[1]])
            records.append({
                "start_sec": decision.silence.start_sec,
                "end_sec": decision.silence.end_sec,
                "mode": decision.mode.value,
                "reason": decision.reason,
                "decided_by": decision.decided_by,
                "score": decision.score,
            })

        cut.remove_spans = _merge_spans(removals)
        cut.silences = records
        modes = {d.mode for d in decisions}
        if not cut.remove_spans:
            cut.pacing_mode = PacingMode.KEEP
        elif modes == {PacingMode.CUT}:
            cut.pacing_mode = PacingMode.CUT
        else:
            cut.pacing_mode = PacingMode.TRIM
        kept = sum(1 for d in decisions if d.mode is PacingMode.KEEP)
        cut.pacing_reason = (
            f"{len(decisions)} silences: {kept} kept as beats, {len(decisions) - kept} compressed or removed"
        )


def _merge_spans(spans: list[list[float]]) -> list[list[float]]:
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _subtitles(
    episode: Episode,
    timeline: Timeline,
    utterances: list[Utterance],
    speaker_reliability: float,
) -> list[SubtitleLine]:
    """Place speech on the output clock, splitting lines that straddle a removal."""
    lines: list[SubtitleLine] = []
    per_speaker_styles = speaker_reliability >= 0.9      # 16장: off when tags are unreliable

    for segment in timeline.segments:
        for utterance in utterances:
            start = max(utterance.start_sec, segment.source_start_sec)
            end = min(utterance.end_sec, segment.source_end_sec)
            if end - start <= 0.05:
                continue
            out_start = segment.out_start_sec + (start - segment.source_start_sec)
            out_end = segment.out_start_sec + (end - segment.source_start_sec)
            text = _clip_text(utterance, start, end)
            if not text:
                continue
            style = None
            if per_speaker_styles and utterance.speaker not in (UNKNOWN_SPEAKER, ""):
                style = "default"
            lines.append(SubtitleLine(
                start_sec=out_start, end_sec=out_end, text=text,
                speaker=utterance.speaker, style=style,
            ))
    lines.sort(key=lambda l: l.start_sec)
    return lines


def _clip_text(utterance: Utterance, start: float, end: float) -> str:
    """Use word timings to keep only the words that survive inside the span."""
    if not utterance.words:
        return utterance.text
    words = [
        w["word"] for w in utterance.words
        if w.get("start") is not None and start - 0.05 <= float(w["start"]) <= end + 0.05
    ]
    return " ".join(w.strip() for w in words if w).strip() or ""


def _note_implausible_plan(ctx: RunContext, episode: Episode) -> None:
    """Catch a plan that cannot describe this source, whatever produced it.

    An episode may legitimately be long, and 2.4 lets it revisit a moment more
    than once, so cuts overlapping is not by itself wrong. Running longer than
    the broadcast it was cut from is: measured on a six-hour source, a
    retrieval fault yielded an episode of 3,830,063 seconds. Nothing noticed,
    because the only length check compares against a hint the operator may
    never have given. This one needs no hint.
    """
    source = ctx.project.duration_sec
    if not source or not episode.planned_duration_sec:
        return
    if episode.planned_duration_sec <= source:
        return
    note = (
        f"planned {episode.planned_duration_sec:.0f}s from a {source:.0f}s source - "
        "an episode cannot outrun its own broadcast, so retrieval or the structure is wrong"
    )
    log.error("%s: %s", episode.episode_id, note)
    episode.notes = f"{episode.notes}\n{note}".strip()
    ctx.report.setdefault("implausible_plans", []).append(
        {"episode_id": episode.episode_id, "planned_sec": episode.planned_duration_sec,
         "source_sec": source, "cuts": len(episode.timeline), "detail": note}
    )


def _note_length_deviation(ctx: RunContext, episode: Episode, structure: dict) -> None:
    hint = ctx.project.length_hint_sec
    if not hint:
        return
    actual = episode.planned_duration_sec
    if actual and abs(actual - hint) / hint > 0.25:
        note = structure.get("length_note") or "the content did not fit the hinted length"
        episode.notes = (
            f"length hint {hint:.0f}s, planned {actual:.0f}s - the hint is not a constraint (2.6). {note}"
        )
        ctx.report.setdefault("length_deviations", []).append({
            "episode_id": episode.episode_id,
            "hint_sec": hint,
            "planned_sec": round(actual, 1),
            "reason": note,
        })


def _write_plan(ctx: RunContext, episode: Episode) -> EditPlan:
    settings = RenderSettings.from_profile(ctx.profile, target_type=episode.target_type)
    plan = EditPlan.from_episode(
        episode,
        ctx.project.file_path,
        render_settings=settings.as_dict(),
        provenance={
            "profile": ctx.profile.name,
            "profile_source": str(ctx.profile.source_path) if ctx.profile.source_path else "",
            "provisional_parameters_used": ctx.profile.touched_provisional(),
            "producer": ctx.producer.name,
        },
    )
    path = ctx.project_dir / "plans" / f"{episode.episode_id}.json"
    plan.save(path)
    ctx.report.setdefault("edit_plans", []).append(str(path))
    return plan
