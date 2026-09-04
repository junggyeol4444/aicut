"""RENDERING: run the edit plan through ffmpeg (10장).

The plan is the only input. If this stage fails, the plan is untouched and the
render can be retried alone (16장) - which is why the failure is recorded on the
episode rather than thrown up the pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aicut.errors import RenderError
from aicut.media.ffmpeg_util import LIBASS_HINT, filter_missing
from aicut.models import Episode
from aicut.pipeline.context import RunContext
from aicut.render.editplan import EditPlan
from aicut.render.ffmpeg import Renderer
from aicut.render.subtitles import SubtitleStyleProfile, write_ass

log = logging.getLogger(__name__)


def run(ctx: RunContext, episodes: list[Episode]) -> list[Episode]:
    rendered: list[Episode] = []
    for episode in episodes:
        try:
            render_episode(ctx, episode)
            rendered.append(episode)
        except RenderError as exc:
            episode.render_status = "failed"
            episode.notes = f"{episode.notes}\nrender failed: {exc}".strip()
            ctx.store.save_episode(episode)
            ctx.report.setdefault("render_failures", []).append(
                {"episode_id": episode.episode_id, "error": str(exc),
                 "note": "the edit plan survives; re-run the render stage alone (16장)"}
            )
            log.error("render failed for %s: %s", episode.episode_id, exc)
    ctx.note("episodes_rendered", len(rendered))
    return rendered


def render_episode(ctx: RunContext, episode: Episode, plan_path: str | Path | None = None) -> Episode:
    plan = (
        EditPlan.load(plan_path)
        if plan_path
        else EditPlan.load(ctx.project_dir / "plans" / f"{episode.episode_id}.json")
    )
    style = SubtitleStyleProfile.load(ctx.profile.get("render.subtitle_style_profile"))
    ass_path = None
    if plan.subtitles:
        ass_path = write_ass(
            plan.subtitles,
            ctx.project_dir / "subtitles" / f"{episode.episode_id}.ass",
            style,
            title=episode.title_candidates[0] if episode.title_candidates else episode.episode_id,
        )

    if ass_path and filter_missing("subtitles"):
        # A working ffmpeg that was built without libass - the plain Homebrew
        # bottle, most static builds. Losing the whole episode over captions
        # would be the wrong trade, so the video is rendered without them and
        # the .ass stays on disk beside it; 2.6 says a departure is reported,
        # never silent, so it goes in the report and on the episode.
        note = (
            f"captions were NOT burned in: this ffmpeg has no 'subtitles' filter. "
            f"The subtitle file is kept at {ass_path} - re-run the render alone after "
            f"installing a build with libass to get them. {LIBASS_HINT}"
        )
        log.warning("%s: %s", episode.episode_id, note)
        if "captions were NOT burned in" not in episode.notes:
            episode.notes = f"{episode.notes}\n{note}".strip()
        ctx.report.setdefault("degraded", []).append(
            {"episode_id": episode.episode_id, "reason": "no_subtitles_filter",
             "detail": note, "subtitle_file": str(ass_path)}
        )
        ass_path = None

    out_path = ctx.project_dir / "output" / f"{episode.episode_id}.mp4"
    renderer = Renderer(ctx.profile, ctx.project_dir / "work")
    renderer.render(plan, out_path, ass_path=ass_path)

    episode.output_mp4_path = str(out_path)
    episode.render_status = "done"
    ctx.store.save_episode(episode)
    return episode
