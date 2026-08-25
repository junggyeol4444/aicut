"""Replaying the pipeline under a candidate profile (17.4 step 2).

The sweep needs to ask "what would the system decide with *these* parameters?"
hundreds of times, and the honest answer requires running the judging stages,
not a formula. This is that replay, and it is built in because requiring every
operator to write their own harness would leave 17.4 as a procedure nobody can
execute.

Two things make the replay affordable:

* the measured signals are read from the run's cache, so no trial decodes the
  source again - only the derived values (tension, silence verdicts) are
  recomputed under the candidate parameters;
* discovery is replayed through the same producer the project uses, and with
  the offline stand-in it costs nothing, which is enough to sweep the
  signal-level parameters that dominate 17.3's pacing score.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aicut.analysis.pacing import PacingJudge, build_silence_contexts
from aicut.analysis.tension import build_tension_curve
from aicut.analysis.vocalburst import build_detector
from aicut.calibration.dataset import Dataset
from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import AicutError
from aicut.llm import Producer, get_producer
from aicut.media.audio import Silence
from aicut.models import PacingMode
from aicut.pipeline import discovery, evaluating
from aicut.pipeline.context import RunContext, SignalBundle
from aicut.pipeline.states import State

log = logging.getLogger(__name__)


class ReplayHarness:
    """Answers the sweep's question for one candidate profile."""

    def __init__(
        self,
        dataset: Dataset,
        *,
        workspace: str | Path,
        project_id: str | None = None,
        producer: Producer | None = None,
    ):
        self.dataset = dataset
        self.workspace = Path(workspace)
        self.store = Store(self.workspace / "aicut.db")
        self.producer = producer or get_producer("mock")
        self.project = self._find_project(project_id)
        self.signals = SignalBundle.load(self.workspace / self.project.project_id / "signals.json")
        self.utterances = self.store.utterances(self.project.project_id)

    def _find_project(self, project_id: str | None):
        if project_id:
            project = self.store.get_project(project_id)
            if project is None:
                raise AicutError(f"unknown project {project_id}")
            return project
        source = str(Path(self.dataset.source_path).resolve())
        for project in reversed(self.store.list_projects()):
            if str(Path(project.file_path).resolve()) == source:
                return project
        raise AicutError(
            f"no processed project for {self.dataset.source_path}. Run it once "
            "(`aicut run <source> --no-render`) so its signals are cached, then sweep."
        )

    def close(self) -> None:
        self.store.close()

    # ---- the sweep's question ---------------------------------------------
    def run(self, profile: CalibrationProfile) -> dict[str, Any]:
        """System verdicts under ``profile``, in the shape 17.3 scores."""
        return {
            "pacing_keeps": self._pacing_keeps(profile),
            "content_spans": self._content_spans(profile),
        }

    def _pacing_keeps(self, profile: CalibrationProfile) -> list[bool]:
        """One keep/cut verdict per silence the dataset labelled, in its order."""
        if not self.dataset.silence_verdicts:
            return []
        tension = self._tension(profile)
        judge = PacingJudge(profile)          # the rule layer alone: no model call per trial
        silences = [Silence(v.start_sec, v.end_sec) for v in self.dataset.silence_verdicts]
        contexts = build_silence_contexts(
            silences, self.utterances, tension, self.signals.motion, profile,
        )
        return [judge.judge(ctx).mode is PacingMode.KEEP for ctx in contexts]

    def _tension(self, profile: CalibrationProfile):
        laughter = None
        detector = build_detector(profile.get("laughter.detector"))
        if detector is not None and self.signals.rms:
            laughter = detector.as_signal(detector.detect(self.signals.rms, self.utterances, profile))
        return build_tension_curve(self.signals.rms, self.utterances, profile, laughter=laughter)

    def _content_spans(self, profile: CalibrationProfile) -> list[list[float]]:
        """Replay discovery and evaluation, and report what would be produced."""
        if not self.dataset.content_spans:
            return []
        ctx = RunContext(
            project=self.project,
            store=self.store,
            profile=profile,
            producer=self.producer,
            workspace=self.workspace,
            signals=self.signals,
        )
        ctx.signals.tension = self._tension(profile)

        events = {e.event_id: e for e in self.store.events(self.project.project_id)}
        if not events:
            return []

        candidates = discovery.run(ctx)
        keepers = evaluating.run(ctx, candidates)
        spans: list[list[float]] = []
        for group in evaluating.group_for_production(keepers):
            moments = [
                (m.source_start_sec, m.source_end_sec)
                for candidate in group
                for event_id in candidate.related_event_ids
                if event_id in events
                for m in events[event_id].mentions
            ]
            if moments:
                spans.append([min(s for s, _ in moments), max(e for _, e in moments)])
        return spans


def build_evaluator(harness: ReplayHarness):
    """A ``profile -> score`` function for :func:`aicut.calibration.sweep.sweep`."""
    from aicut.calibration.metrics import combined_score, score_content_discovery, score_pacing

    human_keeps = [v.kept for v in harness.dataset.silence_verdicts]
    human_spans = [s.as_tuple() for s in harness.dataset.content_spans]

    def evaluate(profile: CalibrationProfile) -> float:
        system = harness.run(profile)
        pacing = score_pacing(system["pacing_keeps"], human_keeps) if human_keeps else None
        found = [(float(a), float(b)) for a, b in system["content_spans"]]
        content = score_content_discovery(found, human_spans) if human_spans else None
        return combined_score(pacing, content)

    return evaluate


def prepare_project(
    dataset: Dataset,
    *,
    workspace: str | Path,
    profile: CalibrationProfile,
    producer: Producer | None = None,
) -> str:
    """Process the dataset's source once, so the sweep has cached signals to replay."""
    from aicut.media.stt import TranscriptFileTranscriber
    from aicut.pipeline.runner import Pipeline

    store = Store(Path(workspace) / "aicut.db")
    try:
        pipeline = Pipeline(store, profile, producer or get_producer("mock"), workspace=workspace)
        project = pipeline.submit(dataset.source_path, channel_ref=dataset.channel_ref)
        transcriber = (
            TranscriptFileTranscriber(dataset.transcript_path) if dataset.transcript_path else None
        )
        result = pipeline.run(project, transcriber=transcriber, stop_after=State.UNDERSTANDING)
        if result.final_state is State.FAILED:
            raise AicutError(f"could not process {dataset.source_path}: {result.report.get('error')}")
        return project.project_id
    finally:
        store.close()
