"""Loop C: performance feedback (12장).

Own-channel metrics only - retention and click-through do not exist for anyone
else's videos (4.2). What comes back is not applied as a rule; it becomes a
strategy update carrying a confidence, which the planner sees as knowledge and
may still override for a content that does not fit the pattern (7.1).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from aicut.intelligence.youtube import YouTubeClient
from aicut.pipeline.context import RunContext

log = logging.getLogger(__name__)


def collect(ctx: RunContext, client: YouTubeClient, *, days: int = 28) -> list[dict[str, Any]]:
    """Pull metrics for every published episode of this project."""
    end = date.today()
    start = end - timedelta(days=days)
    collected: list[dict[str, Any]] = []

    for episode in ctx.store.episodes(ctx.project.project_id):
        video_id = episode.metadata.get("youtube", {}).get("video_id")
        if not video_id or episode.review_status != "published":
            continue
        metrics = client.analytics(video_id, start.isoformat(), end.isoformat())
        metrics["retention_curve"] = client.audience_retention(video_id, start.isoformat(), end.isoformat())
        metrics["structure"] = episode.planned_structure.get("structure_name", "")
        metrics["target_type"] = episode.target_type
        ctx.store.save_performance(episode.episode_id, metrics)
        collected.append({"episode_id": episode.episode_id, "metrics": metrics})
    return collected


def learn(ctx: RunContext, knowledge_path: str | Path | None = None) -> dict[str, Any]:
    """Turn collected metrics into strategy updates and fold them into knowledge."""
    records = []
    for episode in ctx.store.episodes(ctx.project.project_id):
        for row in ctx.store.performance(episode.episode_id):
            records.append({
                "episode_id": episode.episode_id,
                "structure": episode.planned_structure.get("structure_name", ""),
                "target_type": episode.target_type,
                "duration_sec": episode.planned_duration_sec,
                "cut_count": len(episode.timeline),
                "metrics": row["metrics"],
            })
    if not records:
        return {"observations": [], "strategy_updates": []}

    result = ctx.producer.learn_from_performance({"episodes": records})
    if knowledge_path:
        path = Path(knowledge_path)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        existing.setdefault("performance_learning", []).extend(result.get("strategy_updates", []))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
