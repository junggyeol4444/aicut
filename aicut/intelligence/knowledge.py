"""The production knowledge store (4.5).

What comes out of the reference loop is knowledge, not rules. 4.5 is explicit:
these patterns are consulted when planning a new video, never applied as fixed
law, and 7.1 has the planner compare a pattern against the actual content before
using it. So this store hands the planner evidence with support counts attached
and lets the judgement happen there.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProductionKnowledge:
    """Patterns observed across references, with how much support each has."""

    structure_patterns: list[dict[str, Any]] = field(default_factory=list)
    editing_patterns: list[dict[str, Any]] = field(default_factory=list)
    storytelling_patterns: list[dict[str, Any]] = field(default_factory=list)
    scene_selection_patterns: list[dict[str, Any]] = field(default_factory=list)
    title_patterns: list[str] = field(default_factory=list)
    thumbnail_patterns: list[str] = field(default_factory=list)
    subtitle_patterns: list[dict[str, Any]] = field(default_factory=list)
    performance_learning: list[dict[str, Any]] = field(default_factory=list)
    source_output_rules: list[str] = field(default_factory=list)
    sample_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProductionKnowledge":
        known = {f for f in cls().__dict__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load(cls, path: str | Path) -> "ProductionKnowledge":
        file = Path(path)
        if not file.exists():
            return cls()
        return cls.from_dict(json.loads(file.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    def summary_for_planner(self, *, limit: int = 8) -> dict[str, Any]:
        """A compact view for the planner: patterns plus how well supported they are."""
        return {
            "sample_size": self.sample_size,
            "structure": self.structure_patterns[:limit],
            "storytelling": self.storytelling_patterns[:limit],
            "editing": self.editing_patterns[:limit],
            "scene_selection": self.scene_selection_patterns[:limit],
            "titles": self.title_patterns[:limit],
            "thumbnails": self.thumbnail_patterns[:limit],
            "learned_from_own_performance": self.performance_learning[:limit],
            "learned_from_human_edits": self.source_output_rules[:limit],
            "caveat": "observed patterns, not rules; compare against this content before applying (4.5, 7.1)",
        }


def consolidate(analyses: list[dict[str, Any]]) -> ProductionKnowledge:
    """Fold per-video analyses into patterns, counting how often each recurs.

    4.6's point in code form: the value is in what several references share, not
    in reproducing any one of them.
    """
    knowledge = ProductionKnowledge(sample_size=len(analyses))

    def collect(key: str) -> list[dict[str, Any]]:
        counter: Counter[str] = Counter()
        for analysis in analyses:
            section = analysis.get(key)
            if isinstance(section, dict):
                for name, value in section.items():
                    counter[f"{name}={_stringify(value)}"] += 1
            elif isinstance(section, list):
                counter.update(_stringify(v) for v in section)
            elif section:
                counter[_stringify(section)] += 1
        return [
            {"pattern": pattern, "support": count, "share": round(count / max(1, len(analyses)), 3)}
            for pattern, count in counter.most_common(40)
        ]

    knowledge.structure_patterns = collect("structure")
    knowledge.editing_patterns = collect("editing")
    knowledge.storytelling_patterns = collect("storytelling")
    knowledge.scene_selection_patterns = collect("scene_selection")
    knowledge.subtitle_patterns = collect("subtitles")
    knowledge.title_patterns = [p["pattern"] for p in collect("title_pattern")]
    knowledge.thumbnail_patterns = [p["pattern"] for p in collect("thumbnail_pattern")]
    return knowledge


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:200]
    return str(value)[:200]
