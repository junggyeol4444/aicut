"""The pipeline runner (14장).

Drives QUEUED -> PARSING -> UNDERSTANDING -> DISCOVERING -> EVALUATING ->
PLANNING -> RENDERING -> PACKAGED -> REVIEW_PENDING and stops there. PUBLISHED is
never reached by a run: a person has to pass the gate first (11.3).

The run can also stop earlier on purpose. NO_CONTENT means the broadcast held
nothing worth producing, which is a successful outcome and is reported as one
(1.3, 16장). And every stage boundary is a resume point, so a failure costs the
stage, not the work before it (16장).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import PipelineError
from aicut.llm import Producer, get_producer
from aicut.media.stt import Transcriber
from aicut.models import Episode, Project
from aicut.pipeline import discovery, evaluating, packaging, parsing, planning, rendering, review, understanding
from aicut.pipeline.context import RunContext
from aicut.pipeline.states import State

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    """What a run produced, and why - the work report of 15.5 / 22.6."""

    project_id: str
    final_state: State
    episodes: list[Episode] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    review_items: list[review.ReviewItem] = field(default_factory=list)

    @property
    def produced_nothing(self) -> bool:
        return self.final_state is State.NO_CONTENT


class Pipeline:
    def __init__(
        self,
        store: Store,
        profile: CalibrationProfile,
        producer: Producer | None = None,
        *,
        workspace: str | Path = "workspace",
        knowledge: dict[str, Any] | None = None,
    ):
        self.store = store
        self.profile = profile
        self.producer = producer or get_producer("mock")
        self.workspace = Path(workspace)
        self.knowledge = knowledge or {}

    # ---- entry points ------------------------------------------------------
    def submit(
        self,
        file_path: str,
        *,
        length_hint_sec: float | None = None,
        channel_ref: str = "",
    ) -> Project:
        project = Project(
            # Absolute: the edit plan carries this path and is read back later,
            # possibly from another directory (16장 re-runs the render alone).
            # A relative path would resolve against whatever the working
            # directory happened to be then.
            file_path=str(Path(file_path).expanduser().resolve()),
            status=State.QUEUED.value,
            profile_name=self.profile.name,
            channel_ref=channel_ref,
            length_hint_sec=length_hint_sec,
        )
        return self.store.create_project(project)

    def run(
        self,
        project: Project,
        *,
        transcriber: Transcriber | None = None,
        stop_after: State | None = None,
        sample_frames: bool = False,
        render: bool = True,
        context: RunContext | None = None,
    ) -> RunResult:
        started = time.time()
        ctx = context or RunContext(
            project=project,
            store=self.store,
            profile=self.profile,
            producer=self.producer,
            workspace=self.workspace,
        )
        ctx.note("started_at", time.strftime("%Y-%m-%dT%H:%M:%S"))

        try:
            self._advance(ctx, State.PARSING)
            parsing.run(ctx, transcriber)
            if stop_after is State.PARSING:
                return self._finish(ctx, State.PARSING, [], started)

            self._advance(ctx, State.UNDERSTANDING)
            understanding.run(ctx, sample_frames=sample_frames)
            if stop_after is State.UNDERSTANDING:
                return self._finish(ctx, State.UNDERSTANDING, [], started)

            self._advance(ctx, State.DISCOVERING)
            candidates = discovery.run(ctx)
            if not candidates:
                return self._no_content(ctx, "no content candidate was found in this broadcast", started)
            if stop_after is State.DISCOVERING:
                return self._finish(ctx, State.DISCOVERING, [], started)

            self._advance(ctx, State.EVALUATING)
            keepers = evaluating.run(ctx, candidates)
            if not keepers:
                return self._no_content(ctx, "candidates were found but none was worth producing", started)
            groups = evaluating.group_for_production(keepers)
            if not groups:
                return self._no_content(ctx, "every remaining candidate needed a partner it never found", started)
            if stop_after is State.EVALUATING:
                return self._finish(ctx, State.EVALUATING, [], started)

            self._advance(ctx, State.PLANNING)
            episodes = planning.run(ctx, groups, knowledge=self.knowledge)
            if not episodes:
                return self._no_content(ctx, "no episode survived scene retrieval", started)
            if stop_after is State.PLANNING or not render:
                return self._finish(ctx, State.PLANNING, episodes, started)

            self._advance(ctx, State.RENDERING)
            rendered = rendering.run(ctx, episodes)
            if not rendered:
                raise PipelineError("every render failed; the edit plans are kept for a retry (16장)")

            self._advance(ctx, State.PACKAGED)
            packaging.run(ctx, rendered, knowledge=self.knowledge)

            self._advance(ctx, State.REVIEW_PENDING)
            items = review.pending(ctx, rendered)
            result = self._finish(ctx, State.REVIEW_PENDING, rendered, started)
            result.review_items = items
            return result

        except Exception as exc:
            log.exception("project %s failed", project.project_id)
            self.store.set_status(project.project_id, State.FAILED.value, str(exc))
            ctx.note("error", str(exc))
            return self._finish(ctx, State.FAILED, [], started, record_state=False)

    # ---- helpers -----------------------------------------------------------
    def _advance(self, ctx: RunContext, state: State) -> None:
        self.store.set_status(ctx.project.project_id, state.value)
        ctx.project.status = state.value
        log.info("project %s -> %s", ctx.project.project_id, state.value)

    def _no_content(self, ctx: RunContext, reason: str, started: float) -> RunResult:
        """A normal ending, not a failure (16장)."""
        self.store.set_status(ctx.project.project_id, State.NO_CONTENT.value, reason)
        ctx.note("no_content_reason", reason)
        return self._finish(ctx, State.NO_CONTENT, [], started, record_state=False)

    def _finish(
        self,
        ctx: RunContext,
        state: State,
        episodes: list[Episode],
        started: float,
        *,
        record_state: bool = True,
    ) -> RunResult:
        if record_state:
            ctx.project.status = state.value
        ctx.note("elapsed_sec", round(time.time() - started, 1))
        # Report on the profile and producer that actually ran this context - a
        # resumed run may have been handed different ones than the pipeline holds.
        ctx.note("provisional_parameters_used", ctx.profile.touched_provisional())
        ctx.note("producer", ctx.producer.name)
        ctx.note("profile", ctx.profile.name)
        report = build_report(ctx, state, episodes)
        (ctx.project_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return RunResult(
            project_id=ctx.project.project_id,
            final_state=state,
            episodes=episodes,
            report=report,
        )


def build_report(ctx: RunContext, state: State, episodes: list[Episode]) -> dict[str, Any]:
    """The work report of 22.6: what was found, what was made, what was refused."""
    candidates = ctx.store.candidates(ctx.project.project_id)
    return {
        "project_id": ctx.project.project_id,
        "source": ctx.project.file_path,
        "final_state": state.value,
        "candidates_found": len(candidates),
        "decisions": ctx.report.get("decisions", {}),
        "rejections": ctx.report.get("rejections", []),
        "episodes": [
            {
                "episode_id": e.episode_id,
                "target_type": e.target_type,
                "structure": e.planned_structure.get("structure_name", ""),
                "rationale": e.planned_structure.get("rationale", ""),
                "duration_sec": round(e.planned_duration_sec, 1),
                "cuts": len(e.timeline),
                "output": e.output_mp4_path,
                "titles": e.title_candidates,
                "notes": e.notes,
            }
            for e in episodes
        ],
        "signals": {
            "utterances": ctx.report.get("utterance_count"),
            "first_pass_windows": ctx.report.get("first_pass_windows"),
            "second_pass_windows": ctx.report.get("second_pass_windows"),
            "events": ctx.report.get("events"),
            "situation_mix": ctx.report.get("situation_mix", {}),
            "speaker_reliability": ctx.report.get("speaker_reliability"),
        },
        "length_deviations": ctx.report.get("length_deviations", []),
        "render_failures": ctx.report.get("render_failures", []),
        "no_content_reason": ctx.report.get("no_content_reason"),
        "elapsed_sec": ctx.report.get("elapsed_sec"),
        "profile": ctx.report.get("profile"),
        "producer": ctx.report.get("producer"),
        "provisional_parameters_used": ctx.report.get("provisional_parameters_used", []),
        "warning": (
            "some judgement thresholds are still unmeasured guesses (17.5); "
            "run the calibration sweep before trusting these results"
            if ctx.report.get("provisional_parameters_used") else ""
        ),
    }
