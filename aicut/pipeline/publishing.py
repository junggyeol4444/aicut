"""Upload and publication (11.3, 11.4, 16장).

The order is fixed: upload private -> a person reviews -> the video goes public.
An unapproved episode cannot reach the public path from here; that is the gate,
expressed in code rather than in a policy document.

When the quota runs out the episode is not lost. It is kept locally, queued, and
scheduled against the next PT midnight (11.4).
"""

from __future__ import annotations

import logging
from typing import Any

from aicut.errors import QuotaExceeded
from aicut.intelligence.quota import QuotaLedger
from aicut.intelligence.youtube import YouTubeClient
from aicut.models import Episode
from aicut.pipeline.context import RunContext

log = logging.getLogger(__name__)


def upload_episode(
    ctx: RunContext,
    episode: Episode,
    client: YouTubeClient,
    *,
    thumbnail_path: str | None = None,
) -> dict[str, Any]:
    """Upload one episode privately and record the result."""
    if not episode.output_mp4_path:
        raise ValueError(f"episode {episode.episode_id} has not been rendered")

    privacy = ctx.profile.get("upload.privacy_on_upload")
    metadata = {
        "title": (episode.title_candidates or ["untitled"])[0],
        "description": episode.metadata.get("description", ""),
        "tags": episode.metadata.get("tags", []),
    }
    try:
        result = client.upload(episode.output_mp4_path, metadata, privacy_status=privacy)
    except QuotaExceeded as exc:
        ctx.store.enqueue_upload(
            episode.episode_id,
            retry_after=exc.reset_at.isoformat() if exc.reset_at else None,
            error=str(exc),
        )
        episode.review_status = "upload_queued"
        ctx.store.save_episode(episode)
        ctx.report.setdefault("upload_queue", []).append({
            "episode_id": episode.episode_id,
            "retry_after": exc.reset_at.isoformat() if exc.reset_at else None,
            "reason": str(exc),
        })
        log.warning("quota exhausted; %s stays local and is queued", episode.episode_id)
        raise

    chosen = thumbnail_path or (episode.thumbnail_candidates[0] if episode.thumbnail_candidates else None)
    if chosen:
        client.set_thumbnail(result.video_id, chosen)
        episode.thumbnail_path = chosen

    episode.metadata = dict(episode.metadata)
    episode.metadata["youtube"] = {
        "video_id": result.video_id,
        "url": result.url,
        "privacy_status": result.privacy_status,
    }
    episode.review_status = "pending" if ctx.profile.get("upload.require_human_review") else "approved"
    ctx.store.save_episode(episode)
    return episode.metadata["youtube"]


def publish_approved(ctx: RunContext, episode: Episode, client: YouTubeClient) -> Episode:
    """Make a reviewed episode public. Refuses anything the gate has not passed."""
    if episode.review_status != "approved":
        raise PermissionError(
            f"episode {episode.episode_id} is '{episode.review_status}', not 'approved'; "
            "the human review gate has not been passed (11.3)"
        )
    youtube = episode.metadata.get("youtube", {})
    video_id = youtube.get("video_id")
    if not video_id:
        raise ValueError(f"episode {episode.episode_id} has not been uploaded yet")

    client.set_privacy(video_id, "public")
    episode.metadata = dict(episode.metadata)
    episode.metadata["youtube"] = {**youtube, "privacy_status": "public"}
    episode.review_status = "published"
    ctx.store.save_episode(episode)
    return episode


def process_retry_queue(ctx: RunContext, client: YouTubeClient, ledger: QuotaLedger) -> list[str]:
    """Retry queued uploads once the PT day has actually rolled over (11.4)."""
    from datetime import datetime

    now = ledger.pt_now()
    done: list[str] = []
    for row in ctx.store.upload_queue():
        retry_after = row.get("retry_after")
        if retry_after and datetime.fromisoformat(retry_after) > now:
            continue
        episode = ctx.store.get_episode(row["episode_id"])
        if episode is None:
            ctx.store.set_queue_state(row["queue_id"], "abandoned", "episode no longer exists")
            continue
        try:
            upload_episode(ctx, episode, client)
        except QuotaExceeded as exc:
            ctx.store.set_queue_state(row["queue_id"], "RETRY_QUEUED", str(exc))
            break               # the day's allowance is gone again; stop trying
        else:
            ctx.store.set_queue_state(row["queue_id"], "uploaded")
            done.append(episode.episode_id)
    return done
