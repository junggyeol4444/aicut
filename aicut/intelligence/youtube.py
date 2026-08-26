"""YouTube Data / Analytics API access.

Two boundaries this module keeps, both from the document:

**4.2 - what is knowable about someone else's video.** Retention, average view
duration, drop-off points and click-through rate are Analytics API metrics and
exist only for channels you own. For reference videos the ceiling is public
metrics: views, likes, comment count. Reference learning (4장) is therefore built
on public metrics, and retention-based learning (12장) is restricted to the
system's own channel. The API surface here enforces that split rather than
leaving it to discipline.

**4.6 - reference media is not archived.** Bulk-downloading and keeping reference
video files raises terms-of-service and copyright problems, so the analysis path
takes metadata and returns patterns; nothing in this module downloads media.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from aicut.errors import AicutError, QuotaExceeded
from aicut.intelligence.quota import (
    COST_LIST,
    COST_SEARCH,
    COST_THUMBNAIL_SET,
    COST_VIDEO_INSERT,
    QuotaLedger,
)

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


@dataclass
class UploadResult:
    video_id: str
    privacy_status: str
    url: str


class YouTubeClient:
    """Thin wrapper over the Google API client, with quota booked on every call."""

    def __init__(self, credentials: Any, ledger: QuotaLedger):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - optional dep
            raise AicutError("install aicut[youtube] to talk to the YouTube API") from exc
        self._data = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        self._credentials = credentials
        self.ledger = ledger

    # -- public metrics (any video) -----------------------------------------
    def public_metrics(self, video_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Snippet + public statistics. This is the ceiling for other channels (4.2)."""
        ids = list(video_ids)
        out: list[dict[str, Any]] = []
        for chunk_start in range(0, len(ids), 50):
            chunk = ids[chunk_start : chunk_start + 50]
            self._require(COST_LIST, "videos.list")
            response = self._data.videos().list(
                part="snippet,statistics,contentDetails", id=",".join(chunk)
            ).execute()
            self.ledger.spend(COST_LIST, "videos.list")
            for item in response.get("items", []):
                stats = item.get("statistics", {})
                snippet = item.get("snippet", {})
                out.append({
                    "video_id": item["id"],
                    "channel_id": snippet.get("channelId", ""),
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "tags": snippet.get("tags", []),
                    "thumbnails": snippet.get("thumbnails", {}),
                    "duration": item.get("contentDetails", {}).get("duration", ""),
                    "public_metrics": {
                        "views": int(stats.get("viewCount", 0) or 0),
                        "likes": int(stats.get("likeCount", 0) or 0),
                        "comments": int(stats.get("commentCount", 0) or 0),
                    },
                })
        return out

    def search(self, query: str, *, max_results: int = 25, **params: Any) -> list[str]:
        """Find reference video ids. 100 units a call - the expensive one (11.4)."""
        self._require(COST_SEARCH, "search.list")
        response = self._data.search().list(
            part="id", q=query, type="video", maxResults=max_results, **params
        ).execute()
        self.ledger.spend(COST_SEARCH, "search.list")
        return [item["id"]["videoId"] for item in response.get("items", []) if item.get("id", {}).get("videoId")]

    # -- own channel only ----------------------------------------------------
    def analytics(self, video_id: str, start_date: str, end_date: str) -> dict[str, Any]:
        """Retention-grade metrics. Own channel only, by API design (4.2, 12.1)."""
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - optional dep
            raise AicutError("install aicut[youtube] to talk to the YouTube API") from exc
        analytics = build("youtubeAnalytics", "v2", credentials=self._credentials, cache_discovery=False)
        response = analytics.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                    "annotationClickThroughRate,likes,comments,shares",
            filters=f"video=={video_id}",
        ).execute()
        headers = [h["name"] for h in response.get("columnHeaders", [])]
        rows = response.get("rows", [])
        return dict(zip(headers, rows[0])) if rows else {}

    def audience_retention(self, video_id: str, start_date: str, end_date: str) -> list[dict[str, float]]:
        """Elapsed-ratio retention curve: where viewers leave and where they rewatch."""
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - optional dep
            raise AicutError("install aicut[youtube] to talk to the YouTube API") from exc
        analytics = build("youtubeAnalytics", "v2", credentials=self._credentials, cache_discovery=False)
        response = analytics.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="audienceWatchRatio,relativeRetentionPerformance",
            dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}",
        ).execute()
        headers = [h["name"] for h in response.get("columnHeaders", [])]
        return [dict(zip(headers, row)) for row in response.get("rows", [])]

    # -- upload --------------------------------------------------------------
    def upload(
        self,
        video_path: str,
        metadata: dict[str, Any],
        *,
        privacy_status: str = "private",
        category_id: str = "20",
        chunk_size: int = 8 * 1024 * 1024,
    ) -> UploadResult:
        """Upload one video. Defaults to private - the human gate decides the rest (11.3)."""
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:  # pragma: no cover - optional dep
            raise AicutError("install aicut[youtube] to talk to the YouTube API") from exc

        self._require(COST_VIDEO_INSERT, "videos.insert")
        body = {
            "snippet": {
                "title": metadata.get("title", "")[:100],
                "description": metadata.get("description", "")[:5000],
                "tags": metadata.get("tags", [])[:30],
                "categoryId": category_id,
            },
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(video_path, chunksize=chunk_size, resumable=True)
        request = self._data.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
        self.ledger.spend(COST_VIDEO_INSERT, "videos.insert")
        video_id = response["id"]
        return UploadResult(
            video_id=video_id,
            privacy_status=privacy_status,
            url=f"https://www.youtube.com/watch?v={video_id}",
        )

    def set_thumbnail(self, video_id: str, image_path: str) -> None:
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:  # pragma: no cover - optional dep
            raise AicutError("install aicut[youtube] to talk to the YouTube API") from exc
        self._require(COST_THUMBNAIL_SET, "thumbnails.set")
        self._data.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(image_path)).execute()
        self.ledger.spend(COST_THUMBNAIL_SET, "thumbnails.set")

    def set_privacy(self, video_id: str, privacy_status: str) -> None:
        """Flip a reviewed video to public - the only step that makes it visible."""
        self._require(COST_VIDEO_INSERT // 32, "videos.update")
        self._data.videos().update(
            part="status", body={"id": video_id, "status": {"privacyStatus": privacy_status}}
        ).execute()
        self.ledger.spend(COST_VIDEO_INSERT // 32, "videos.update")

    # -- helpers -------------------------------------------------------------
    def _require(self, units: int, what: str) -> None:
        if not self.ledger.can_afford(units):
            reset = self.ledger.next_reset()
            raise QuotaExceeded(
                f"{what} needs {units} units; {self.ledger.state().remaining} left today. "
                f"Next reset {reset.isoformat()} (PT midnight, 11.4).",
                reset_at=reset,
            )


def load_credentials(client_secrets: str, token_path: str):  # pragma: no cover - interactive
    """OAuth for a desktop app, caching the token next to the workspace."""
    import json
    from pathlib import Path

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    token = Path(token_path)
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_info(
            json.loads(token.read_text(encoding="utf-8")), SCOPES
        )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES).run_local_server(port=0)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(creds.to_json(), encoding="utf-8")
    return creds
