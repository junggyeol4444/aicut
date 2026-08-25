"""PACKAGED: thumbnails and metadata for a finished video (11장).

Nothing here is templated. Thumbnail frames are scored out of the finished video
and offered as candidates for a person to choose from (11.1, 15.5); titles,
description, tags and chapters are written for this video's content, informed by
the reference patterns of 4.5 rather than filled into a fixed form (11.2).
"""

from __future__ import annotations

import json
import logging

from aicut.analysis.tension import TensionCurve, build_tension_curve
from aicut.media import audio as audio_mod
from aicut.media import vision as vision_mod
from aicut.media.ffmpeg_util import have_ffmpeg
from aicut.models import Episode
from aicut.pipeline.context import RunContext
from aicut.render import thumbnails
from aicut.render.timeline import Timeline

log = logging.getLogger(__name__)


def run(ctx: RunContext, episodes: list[Episode], *, knowledge: dict | None = None) -> list[Episode]:
    for episode in episodes:
        package_episode(ctx, episode, knowledge=knowledge)
    ctx.note("episodes_packaged", len(episodes))
    return episodes


def package_episode(ctx: RunContext, episode: Episode, *, knowledge: dict | None = None) -> Episode:
    timeline = Timeline.from_cuts(episode.timeline)
    boundaries = timeline.cut_boundaries()
    candidates = ctx.store.candidates(ctx.project.project_id)
    core = " / ".join(c.core_summary for c in candidates if c.candidate_id in episode.candidate_ids)

    if episode.output_mp4_path and have_ffmpeg():
        episode.thumbnail_candidates = [c.path for c in _thumbnails(ctx, episode)]

    answer = ctx.producer.package_metadata({
        "core_summary": core,
        "structure": episode.planned_structure,
        "target_type": episode.target_type,
        "duration_sec": timeline.duration,
        "cuts": [
            {
                "sequence_order": c.sequence_order,
                "scene_role": c.scene_role,
                "speaker": c.speaker_tag,
                # Where this cut begins in the finished video - the only places a
                # chapter mark can honestly sit.
                "output_start_sec": round(at, 2),
            }
            for c, at in zip(sorted(episode.timeline, key=lambda c: c.sequence_order), boundaries)
        ],
        "subtitles": [{"at_sec": s.start_sec, "text": s.text} for s in episode.subtitles[:200]],
        "youtube_knowledge": knowledge or {},
    })

    titles = [t for t in (answer.get("titles") or []) if t][:3]
    episode.title_candidates = titles
    episode.metadata = {
        "titles": titles,
        "description": answer.get("description", ""),
        "tags": answer.get("tags", []) or [],
        "chapters": answer.get("chapters", []) or [],
        "duration_sec": round(timeline.duration, 2),
    }
    path = ctx.project_dir / "metadata" / f"{episode.episode_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(episode.metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    ctx.store.save_episode(episode)
    return episode


def _thumbnails(ctx: RunContext, episode: Episode) -> list[thumbnails.ThumbnailCandidate]:
    """Score the finished file, then extract the top frames at full quality."""
    video = episode.output_mp4_path
    try:
        rms = audio_mod.rms_envelope(video)
        motion = vision_mod.motion_curve(video, interval_sec=1.0)
    except Exception as exc:                     # measurement is optional, the video is not
        log.warning("thumbnail scoring skipped for %s: %s", episode.episode_id, exc)
        return []
    curve: TensionCurve = build_tension_curve(rms, [], ctx.profile)
    duration = curve.times[-1] if curve.times else 0.0
    picked = thumbnails.score_frames(duration, curve, motion, ctx.profile)
    return thumbnails.extract(video, picked, ctx.project_dir / "thumbnails" / episode.episode_id)
