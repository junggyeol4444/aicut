"""REVIEW_PENDING: the human gate (11.3), and the candidate review screen (15.4).

11.3 replaces the zero-touch design with a mandatory checkpoint: a finished video
is uploaded private and stays private until a person releases it. Automatic
release exists only as an explicit opt-in for a system that has already proven
itself, and the opt-in is recorded on the decision so it is never ambiguous who
allowed a video out.

The other half of this module is 15.4: the reviewer's agreement or disagreement
with each discovery decision is captured, because that record is training data
for loop B (12.3) and the raw material of the MVP 3 acceptance test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aicut.models import Decision, Episode
from aicut.pipeline.context import RunContext


@dataclass
class ReviewItem:
    episode_id: str
    titles: list[str]
    thumbnail_candidates: list[str]
    output_path: str | None
    duration_sec: float
    structure: dict[str, Any]
    plan_path: str
    notes: str = ""
    provisional_parameters: list[str] = field(default_factory=list)


def pending(ctx: RunContext, episodes: list[Episode]) -> list[ReviewItem]:
    """Everything waiting on a person, with what they need to judge it."""
    items = []
    for episode in episodes:
        episode.review_status = "pending"
        ctx.store.save_episode(episode)
        items.append(ReviewItem(
            episode_id=episode.episode_id,
            titles=episode.title_candidates,
            thumbnail_candidates=episode.thumbnail_candidates,
            output_path=episode.output_mp4_path,
            duration_sec=episode.planned_duration_sec,
            structure=episode.planned_structure,
            plan_path=str(ctx.project_dir / "plans" / f"{episode.episode_id}.json"),
            notes=episode.notes,
            provisional_parameters=ctx.profile.touched_provisional(),
        ))
    return items


def approve(ctx: RunContext, episode_id: str, *, reviewer: str, note: str = "") -> Episode:
    """Release one episode for publication. Without this, nothing goes public."""
    episode = ctx.store.get_episode(episode_id)
    if episode is None:
        raise KeyError(f"unknown episode {episode_id}")
    episode.review_status = "approved"
    episode.metadata = dict(episode.metadata)
    episode.metadata["review"] = {
        "by": reviewer,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
        "auto": False,
    }
    ctx.store.save_episode(episode)
    return episode


def reject(ctx: RunContext, episode_id: str, *, reviewer: str, reason: str) -> Episode:
    episode = ctx.store.get_episode(episode_id)
    if episode is None:
        raise KeyError(f"unknown episode {episode_id}")
    episode.review_status = "rejected"
    episode.metadata = dict(episode.metadata)
    episode.metadata["review"] = {
        "by": reviewer,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": reason,
        "auto": False,
    }
    ctx.store.save_episode(episode)
    return episode


def record_candidate_verdict(ctx: RunContext, candidate_id: str, verdict: str, note: str = "") -> None:
    """15.4: a person agrees or disagrees with a discovery decision.

    Kept verbatim - agreement and disagreement are equally informative, and this
    is the only source of ground truth the system gets about 6장's judgements
    short of a full source/output pair.
    """
    if verdict not in ("agree", "disagree"):
        raise ValueError("verdict must be 'agree' or 'disagree'")
    ctx.store.set_human_verdict(candidate_id, verdict if not note else f"{verdict}: {note}")


def candidate_review(ctx: RunContext) -> list[dict[str, Any]]:
    """The 15.4 screen: every candidate, the decision, and why."""
    return [
        {
            "candidate_id": c.candidate_id,
            "core_summary": c.core_summary,
            "decision": c.decision.value,
            "reason": c.decision_reason,
            "independence_score": c.independence_score,
            "density_score": c.density_score,
            "has_resolution": c.has_resolution,
            "required_context": c.required_context,
            "human_verdict": c.human_verdict,
            "events": c.related_event_ids,
        }
        for c in ctx.store.candidates(ctx.project.project_id)
    ]


def agreement_rate(ctx: RunContext) -> dict[str, float]:
    """How often the human agreed - the number MVP 3 is judged on."""
    candidates = [c for c in ctx.store.candidates(ctx.project.project_id) if c.human_verdict]
    if not candidates:
        return {"reviewed": 0, "agreement": 0.0}
    agreed = sum(1 for c in candidates if c.human_verdict.startswith("agree"))
    produced = [c for c in candidates if c.decision is Decision.PRODUCE]
    false_positives = sum(1 for c in produced if c.human_verdict.startswith("disagree"))
    return {
        "reviewed": len(candidates),
        "agreement": round(agreed / len(candidates), 3),
        "false_positive_rate": round(false_positives / len(produced), 3) if produced else 0.0,
    }
