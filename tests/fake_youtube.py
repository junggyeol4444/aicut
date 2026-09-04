"""A stand-in for the YouTube API, honouring the parts the code depends on.

Enough to exercise the upload, quota and learning paths without a network or a
Google project: it books quota through the real :class:`QuotaLedger`, so quota
exhaustion happens the way it happens in production, and it enforces the 4.2
split - public metrics for anyone, retention only for the owner's channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aicut.errors import QuotaExceeded
from aicut.intelligence.quota import (
    COST_LIST,
    COST_SEARCH,
    COST_THUMBNAIL_SET,
    COST_VIDEO_INSERT,
    QuotaLedger,
)
from aicut.intelligence.youtube import UploadResult


@dataclass
class FakeYouTubeClient:
    ledger: QuotaLedger
    uploaded: list[dict[str, Any]] = field(default_factory=list)
    thumbnails: dict[str, str] = field(default_factory=dict)
    privacy: dict[str, str] = field(default_factory=dict)
    catalogue: dict[str, dict[str, Any]] = field(default_factory=dict)
    _counter: int = 0

    # -- quota gate, same shape as the real client -------------------------
    def _require(self, units: int, what: str) -> None:
        if not self.ledger.can_afford(units):
            reset = self.ledger.next_reset()
            raise QuotaExceeded(
                f"{what} needs {units} units; {self.ledger.state().remaining} left today. "
                f"Next reset {reset.isoformat()} (PT midnight, 11.4).",
                reset_at=reset,
            )

    # -- public surface ----------------------------------------------------
    def search(self, query: str, *, max_results: int = 25, **params: Any) -> list[str]:
        self._require(COST_SEARCH, "search.list")
        self.ledger.spend(COST_SEARCH, "search.list")
        return [vid for vid, row in self.catalogue.items() if query in row.get("title", "")][:max_results]

    def public_metrics(self, video_ids) -> list[dict[str, Any]]:
        ids = list(video_ids)
        self._require(COST_LIST, "videos.list")
        self.ledger.spend(COST_LIST, "videos.list")
        return [self.catalogue[v] for v in ids if v in self.catalogue]

    def upload(self, video_path: str, metadata: dict[str, Any], *, privacy_status: str = "private",
               **kwargs: Any) -> UploadResult:
        self._require(COST_VIDEO_INSERT, "videos.insert")
        self.ledger.spend(COST_VIDEO_INSERT, "videos.insert")
        self._counter += 1
        video_id = f"vid{self._counter:03d}"
        self.uploaded.append({"video_id": video_id, "path": video_path, "metadata": metadata,
                              "privacy": privacy_status})
        self.privacy[video_id] = privacy_status
        return UploadResult(video_id=video_id, privacy_status=privacy_status,
                            url=f"https://www.youtube.com/watch?v={video_id}")

    def set_thumbnail(self, video_id: str, image_path: str) -> None:
        self._require(COST_THUMBNAIL_SET, "thumbnails.set")
        self.ledger.spend(COST_THUMBNAIL_SET, "thumbnails.set")
        self.thumbnails[video_id] = image_path

    def set_privacy(self, video_id: str, privacy_status: str) -> None:
        self.privacy[video_id] = privacy_status

    # -- own channel only (4.2) --------------------------------------------
    def analytics(self, video_id: str, start_date: str, end_date: str) -> dict[str, Any]:
        if video_id not in self.privacy:
            raise PermissionError("analytics is only available for the owner's own videos (4.2)")
        return {"views": 1200, "averageViewPercentage": 41.5, "estimatedMinutesWatched": 300}

    def audience_retention(self, video_id: str, start_date: str, end_date: str) -> list[dict[str, float]]:
        if video_id not in self.privacy:
            raise PermissionError("retention is only available for the owner's own videos (4.2)")
        return [{"elapsedVideoTimeRatio": r / 10, "audienceWatchRatio": 1.0 - r / 20} for r in range(11)]
