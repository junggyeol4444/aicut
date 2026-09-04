"""The edit plan: the contract between judgement and execution (8.2, 10.1).

Planning writes this file and stops. Rendering reads this file and nothing else.
The split is what makes 16장's "a failed render must not cost the plan" possible,
what lets MVP 5 ship before MVP 6 exists, and what lets a human open the plan,
disagree with a decision and change it without touching code (22.5).

Every judgement carries the reason that produced it, because a plan a reviewer
cannot argue with is not reviewable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aicut.errors import PlanValidationError
from aicut.models import Cut, Episode, PacingMode, SubtitleLine

SCHEMA_VERSION = "1"


@dataclass
class EditPlan:
    episode_id: str
    project_id: str
    source_path: str
    cuts: list[Cut] = field(default_factory=list)
    subtitles: list[SubtitleLine] = field(default_factory=list)
    structure: dict[str, Any] = field(default_factory=dict)
    target_type: str = ""
    planned_duration_sec: float = 0.0
    render_settings: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    # ---- conversion --------------------------------------------------------
    @classmethod
    def from_episode(
        cls,
        episode: Episode,
        source_path: str,
        *,
        render_settings: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> "EditPlan":
        return cls(
            episode_id=episode.episode_id,
            project_id=episode.project_id,
            source_path=source_path,
            cuts=list(episode.timeline),
            subtitles=list(episode.subtitles),
            structure=episode.planned_structure,
            target_type=episode.target_type,
            planned_duration_sec=episode.planned_duration_sec,
            render_settings=render_settings or {},
            metadata=episode.metadata,
            provenance=provenance or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "project_id": self.project_id,
            "source_path": self.source_path,
            "target_type": self.target_type,
            "planned_duration_sec": round(self.planned_duration_sec, 3),
            "structure": self.structure,
            "provenance": self.provenance,
            "render": self.render_settings,
            "metadata": self.metadata,
            "cuts": [
                {
                    "sequence_order": c.sequence_order,
                    "source_start_sec": round(c.source_start_sec, 3),
                    "source_end_sec": round(c.source_end_sec, 3),
                    "speaker_tag": c.speaker_tag,
                    "scene_role": c.scene_role,
                    "pacing_mode": c.pacing_mode.value,
                    "pacing_reason": c.pacing_reason,
                    "remove_spans": [[round(a, 3), round(b, 3)] for a, b in (tuple(s) for s in c.remove_spans)],
                    "visual_effect": c.visual_effect,
                    "audio_effect": c.audio_effect,
                    "subtitle_ref": c.subtitle_ref,
                }
                for c in sorted(self.cuts, key=lambda c: c.sequence_order)
            ],
            "subtitles": [
                {
                    "start_sec": round(s.start_sec, 3),
                    "end_sec": round(s.end_sec, 3),
                    "text": s.text,
                    "speaker": s.speaker,
                    "emphasis": s.emphasis,
                    "style": s.style,
                }
                for s in sorted(self.subtitles, key=lambda s: s.start_sec)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditPlan":
        validate(data)
        cuts = [
            Cut(
                sequence_order=int(c["sequence_order"]),
                source_start_sec=float(c["source_start_sec"]),
                source_end_sec=float(c["source_end_sec"]),
                speaker_tag=c.get("speaker_tag", "UNKNOWN"),
                scene_role=c.get("scene_role", ""),
                pacing_mode=PacingMode(c.get("pacing_mode", "TRIM")),
                pacing_reason=c.get("pacing_reason", ""),
                remove_spans=[list(map(float, span)) for span in c.get("remove_spans", [])],
                visual_effect=c.get("visual_effect", {}) or {},
                audio_effect=c.get("audio_effect", {}) or {},
                subtitle_ref=c.get("subtitle_ref"),
            )
            for c in data["cuts"]
        ]
        subtitles = [
            SubtitleLine(
                start_sec=float(s["start_sec"]),
                end_sec=float(s["end_sec"]),
                text=s["text"],
                speaker=s.get("speaker", "UNKNOWN"),
                emphasis=bool(s.get("emphasis", False)),
                style=s.get("style"),
            )
            for s in data.get("subtitles", [])
        ]
        return cls(
            episode_id=data["episode_id"],
            project_id=data.get("project_id", ""),
            source_path=data["source_path"],
            cuts=cuts,
            subtitles=subtitles,
            structure=data.get("structure", {}),
            target_type=data.get("target_type", ""),
            planned_duration_sec=float(data.get("planned_duration_sec", 0.0)),
            render_settings=data.get("render", {}),
            metadata=data.get("metadata", {}),
            provenance=data.get("provenance", {}),
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
        )

    # ---- io ----------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "EditPlan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def validate(data: dict[str, Any]) -> None:
    """Reject a plan the renderer could not execute, naming the offending cut."""
    for key in ("episode_id", "source_path", "cuts"):
        if key not in data:
            raise PlanValidationError(f"edit plan is missing required key {key!r}")
    if str(data.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
        raise PlanValidationError(
            f"edit plan schema version {data.get('schema_version')!r} != supported {SCHEMA_VERSION!r}"
        )
    if not isinstance(data["cuts"], list):
        raise PlanValidationError("'cuts' must be a list")

    orders: set[int] = set()
    for i, cut in enumerate(data["cuts"]):
        where = f"cuts[{i}]"
        for key in ("sequence_order", "source_start_sec", "source_end_sec"):
            if key not in cut:
                raise PlanValidationError(f"{where} is missing {key!r}")
        order = int(cut["sequence_order"])
        if order in orders:
            raise PlanValidationError(f"{where} repeats sequence_order {order}")
        orders.add(order)
        start, end = float(cut["source_start_sec"]), float(cut["source_end_sec"])
        if end <= start:
            raise PlanValidationError(f"{where} has a non-positive duration ({start} -> {end})")
        if start < 0:
            raise PlanValidationError(f"{where} starts before the source begins")
        mode = cut.get("pacing_mode", "TRIM")
        if mode not in (m.value for m in PacingMode):
            raise PlanValidationError(f"{where} has unknown pacing_mode {mode!r}")
        for span in cut.get("remove_spans", []):
            if len(span) != 2:
                raise PlanValidationError(f"{where} has a malformed remove_span {span!r}")
            a, b = float(span[0]), float(span[1])
            if not (start <= a < b <= end):
                raise PlanValidationError(f"{where} remove_span {span!r} falls outside the cut")

    for i, line in enumerate(data.get("subtitles", [])):
        if float(line.get("end_sec", 0)) <= float(line.get("start_sec", 0)):
            raise PlanValidationError(f"subtitles[{i}] has a non-positive duration")


def describe(plan: EditPlan) -> str:
    """Human-readable summary - the MVP 5 success test is a person reading this."""
    lines = [
        f"episode {plan.episode_id}",
        f"  source     : {plan.source_path}",
        f"  structure  : {plan.structure.get('structure_name', '(unnamed)')}"
        f" - {plan.structure.get('rationale', '')}",
        f"  form       : {plan.target_type}, planned {plan.planned_duration_sec:.1f}s,"
        f" {len(plan.cuts)} cuts, {len(plan.subtitles)} subtitle lines",
    ]
    if plan.structure.get("length_note"):
        lines.append(f"  length note: {plan.structure['length_note']}")
    for cut in sorted(plan.cuts, key=lambda c: c.sequence_order):
        removed = sum(b - a for a, b in (tuple(s) for s in cut.remove_spans))
        lines.append(
            f"  [{cut.sequence_order:02d}] {_hms(cut.source_start_sec)}-{_hms(cut.source_end_sec)}"
            f" ({cut.source_duration:.1f}s{f', -{removed:.1f}s' if removed else ''})"
            f" role={cut.scene_role or '-'} pacing={cut.pacing_mode.value}"
            f" speaker={cut.speaker_tag}"
        )
        if cut.pacing_reason:
            lines.append(f"        pacing: {cut.pacing_reason}")
        if cut.visual_effect:
            lines.append(f"        visual: {cut.visual_effect}")
    return "\n".join(lines)


def _hms(seconds: float) -> str:
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
