"""The calibration dataset of 17.2.

17.2 asks for three things: the operator's own broadcast, the video a human
actually cut from it, and the correspondence between them. 17.3 then scores the
system against that correspondence. None of it happens without somewhere to put
the labels, so this is that place - and the reason it exists as code is that
17.2 is the real bottleneck of the whole project: every threshold stays a guess
until a dataset exists, and a dataset nobody can build is a dataset that never
does.

Two kinds of label, matching the two things 17.3 measures:

* **content spans** - "a person would make a video out of this stretch", which
  scores discovery (6장) for recall and false positives.
* **silence verdicts** - "a person kept this pause / cut it", which scores
  pacing (9장). These can be derived from a human edit instead of typed in:
  a silence that survived into the finished video was kept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from aicut.errors import AicutError

SCHEMA_VERSION = "1"


@dataclass
class ContentSpan:
    """A stretch a person would turn into a video."""

    start_sec: float
    end_sec: float
    note: str = ""

    def as_tuple(self) -> tuple[float, float]:
        return (self.start_sec, self.end_sec)


@dataclass
class SilenceVerdict:
    """What a person did with one silence: kept it as a beat, or cut it."""

    start_sec: float
    end_sec: float
    kept: bool
    note: str = ""


@dataclass
class Dataset:
    """One source broadcast, labelled (17.2)."""

    source_path: str
    transcript_path: str | None = None
    output_path: str | None = None
    channel_ref: str = ""
    content_spans: list[ContentSpan] = field(default_factory=list)
    silence_verdicts: list[SilenceVerdict] = field(default_factory=list)
    notes: str = ""
    schema_version: str = SCHEMA_VERSION

    # ---- io ----------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_path": self.source_path,
            "transcript_path": self.transcript_path,
            "output_path": self.output_path,
            "channel_ref": self.channel_ref,
            "notes": self.notes,
            "content_spans": [
                {"start_sec": s.start_sec, "end_sec": s.end_sec, "note": s.note}
                for s in sorted(self.content_spans, key=lambda s: s.start_sec)
            ],
            "silence_verdicts": [
                {"start_sec": v.start_sec, "end_sec": v.end_sec, "kept": v.kept, "note": v.note}
                for v in sorted(self.silence_verdicts, key=lambda v: v.start_sec)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Dataset":
        version = str(data.get("schema_version", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise AicutError(f"dataset schema {version!r} != supported {SCHEMA_VERSION!r}")
        if not data.get("source_path"):
            raise AicutError("a dataset must name the source broadcast it labels")
        return cls(
            source_path=data["source_path"],
            transcript_path=data.get("transcript_path"),
            output_path=data.get("output_path"),
            channel_ref=data.get("channel_ref", ""),
            notes=data.get("notes", ""),
            content_spans=[
                ContentSpan(float(s["start_sec"]), float(s["end_sec"]), s.get("note", ""))
                for s in data.get("content_spans", [])
            ],
            silence_verdicts=[
                SilenceVerdict(float(v["start_sec"]), float(v["end_sec"]), bool(v["kept"]), v.get("note", ""))
                for v in data.get("silence_verdicts", [])
            ],
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Dataset":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- labelling ---------------------------------------------------------
    def add_content(self, start_sec: float, end_sec: float, note: str = "") -> ContentSpan:
        if end_sec <= start_sec:
            raise AicutError(f"a content span must have a positive length ({start_sec} -> {end_sec})")
        span = ContentSpan(start_sec, end_sec, note)
        self.content_spans.append(span)
        return span

    def add_silence_verdict(self, start_sec: float, end_sec: float, kept: bool, note: str = "") -> SilenceVerdict:
        if end_sec <= start_sec:
            raise AicutError(f"a silence must have a positive length ({start_sec} -> {end_sec})")
        verdict = SilenceVerdict(start_sec, end_sec, kept, note)
        self.silence_verdicts.append(verdict)
        return verdict

    # ---- derivation from a human edit --------------------------------------
    def derive_silence_verdicts(
        self,
        silences: Sequence[Any],
        alignment,
        *,
        survival_ratio: float = 0.6,
    ) -> list[SilenceVerdict]:
        """Read the pacing verdicts out of what a human actually kept (12.3 B).

        The evidence is not whether the pause is inside a kept span - a pause
        sits *between* two lines, not inside one. It is how much of the gap
        survived: take the two lines the silence sits between, and if both were
        carried into the finished video, compare the gap they have there with
        the gap they had in the source. A gap that survived largely intact was
        kept as a beat; one that collapsed was cut. A silence next to a line the
        editor dropped is cut with it.

        This is what makes 17.2 affordable. The labels come out of an edit that
        already exists rather than a person marking hundreds of pauses by hand,
        and they carry the distinction 17.3 actually scores.
        """
        kept = sorted(
            (s for s in alignment.kept_spans if s.output_start_sec is not None),
            key=lambda s: s.source_start_sec,
        )
        verdicts: list[SilenceVerdict] = []

        for silence in silences:
            start, end = float(silence.start_sec), float(silence.end_sec)
            source_gap = max(1e-6, end - start)

            before = [s for s in kept if s.source_end_sec <= start + 0.25]
            after = [s for s in kept if s.source_start_sec >= end - 0.25]
            if not before or not after:
                verdicts.append(SilenceVerdict(
                    start, end, kept=False,
                    note="the editor dropped the material on one side of this pause",
                ))
                continue

            previous, following = before[-1], after[0]
            output_gap = following.output_start_sec - previous.output_end_sec
            survived = output_gap / source_gap
            verdicts.append(SilenceVerdict(
                start_sec=start,
                end_sec=end,
                kept=survived >= survival_ratio,
                note=f"{survived:.0%} of the gap survived the human edit",
            ))

        self.silence_verdicts = verdicts
        return verdicts

    # ---- reporting ---------------------------------------------------------
    def coverage(self) -> dict[str, Any]:
        labelled = sum(s.end_sec - s.start_sec for s in self.content_spans)
        return {
            "content_spans": len(self.content_spans),
            "labelled_sec": round(labelled, 1),
            "silence_verdicts": len(self.silence_verdicts),
            "kept_silences": sum(1 for v in self.silence_verdicts if v.kept),
            "has_output": bool(self.output_path),
            "ready_for": _ready_for(self),
        }


def _ready_for(dataset: Dataset) -> list[str]:
    """Which 17.3 metrics this dataset can actually score."""
    ready = []
    if dataset.content_spans:
        ready.append("content discovery (recall, precision, false positives)")
    if dataset.silence_verdicts:
        ready.append("pacing (keep recall, cut precision)")
    if not ready:
        ready.append("nothing yet - label some content spans or derive silence verdicts")
    return ready
