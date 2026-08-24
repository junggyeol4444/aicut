"""The object every stage reads from and writes to.

Signals measured in PARSING (tension curve, motion, silences) are cached to the
workspace so a later stage - or a re-run of one stage after a failure (16장) -
does not have to decode six hours of video again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aicut.analysis.tension import TensionCurve
from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.llm import Producer
from aicut.media.faces import FaceReading
from aicut.media.probe import MediaInfo
from aicut.media.audio import Silence
from aicut.media.vision import MotionSample
from aicut.models import Project


@dataclass
class SignalBundle:
    """Everything measured off the media, in one cacheable object."""

    tension: TensionCurve = field(default_factory=TensionCurve)
    motion: list[MotionSample] = field(default_factory=list)
    silences: list[Silence] = field(default_factory=list)
    faces: list[FaceReading] = field(default_factory=list)
    """Face readings from the sampled frames. Empty when no detector was available -
    callers must degrade rather than assume a value (5.3, 11.1)."""
    rms: list[tuple[float, float]] = field(default_factory=list)
    """Raw ``(time_sec, level_db)``, kept so the tension curve can be rebuilt
    with different profile weights without decoding the source again (17.4)."""
    speaker_reliability: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tension": {
                "times": [round(t, 3) for t in self.tension.times],
                "values": [round(v, 4) for v in self.tension.values],
                "frame_sec": self.tension.frame_sec,
                "scale": list(self.tension.scale),
            },
            "motion": [{"at_sec": round(m.at_sec, 3), "score": round(m.score, 4)} for m in self.motion],
            "silences": [{"start_sec": round(s.start_sec, 3), "end_sec": round(s.end_sec, 3)} for s in self.silences],
            "rms": [[round(t, 3), round(level, 2)] for t, level in self.rms],
            "faces": [
                {"at_sec": round(f.at_sec, 3), "face_ratio": round(f.face_ratio, 4),
                 "face_count": f.face_count, "box": list(f.box) if f.box else None}
                for f in self.faces
            ],
            "speaker_reliability": self.speaker_reliability,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignalBundle":
        tension = data.get("tension", {})
        return cls(
            tension=TensionCurve(
                times=list(tension.get("times", [])),
                values=list(tension.get("values", [])),
                frame_sec=float(tension.get("frame_sec", 1.0)),
                scale=tuple(tension.get("scale", (-60.0, -10.0))),  # type: ignore[arg-type]
            ),
            motion=[MotionSample(at_sec=m["at_sec"], score=m["score"]) for m in data.get("motion", [])],
            silences=[Silence(start_sec=s["start_sec"], end_sec=s["end_sec"]) for s in data.get("silences", [])],
            rms=[(float(t), float(level)) for t, level in data.get("rms", [])],
            faces=[
                FaceReading(
                    at_sec=f["at_sec"], face_ratio=f["face_ratio"],
                    face_count=f.get("face_count", 0),
                    box=tuple(f["box"]) if f.get("box") else None,
                )
                for f in data.get("faces", [])
            ],
            speaker_reliability=float(data.get("speaker_reliability", 0.0)),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "SignalBundle":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class RunContext:
    project: Project
    store: Store
    profile: CalibrationProfile
    producer: Producer
    workspace: Path
    media: MediaInfo | None = None
    signals: SignalBundle = field(default_factory=SignalBundle)
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def project_dir(self) -> Path:
        path = self.workspace / self.project.project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def signal_cache_path(self) -> Path:
        return self.project_dir / "signals.json"

    def note(self, key: str, value: Any) -> None:
        self.report[key] = value
