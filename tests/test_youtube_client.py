"""The real YouTube client, with the Google libraries stubbed out.

What matters here is the request the client builds and the quota it books - the
parts that decide whether a real upload is accepted or rejected - plus the 4.2
boundary between what is knowable about anyone's video and what is knowable only
about your own.
"""

from __future__ import annotations

import sys
import types
import unittest

from aicut.db.store import Store
from aicut.errors import QuotaExceeded
from aicut.intelligence.quota import COST_VIDEO_INSERT, QuotaLedger


class _Request:
    def __init__(self, recorder, kind, kwargs, result):
        self._recorder = recorder
        self._kind = kind
        self._kwargs = kwargs
        self._result = result
        self._chunks = 0

    def execute(self):
        self._recorder.append((self._kind, self._kwargs))
        return self._result

    def next_chunk(self):
        """Two chunks, mirroring a resumable upload that does not finish at once."""
        self._recorder.append((self._kind, self._kwargs))
        self._chunks += 1
        if self._chunks < 2:
            return (None, None)
        return (None, self._result)


class _Resource:
    def __init__(self, recorder, responses):
        self._recorder = recorder
        self._responses = responses

    def __getattr__(self, name):
        def endpoint():
            return _Endpoint(self._recorder, name, self._responses)
        return endpoint


class _Endpoint:
    def __init__(self, recorder, resource, responses):
        self._recorder = recorder
        self._resource = resource
        self._responses = responses

    def __getattr__(self, method):
        def call(**kwargs):
            key = f"{self._resource}.{method}"
            return _Request(self._recorder, key, kwargs, self._responses.get(key, {}))
        return call


def _install_google(responses, recorder):
    discovery = types.ModuleType("googleapiclient.discovery")
    http = types.ModuleType("googleapiclient.http")
    package = types.ModuleType("googleapiclient")

    def build(service, version, credentials=None, cache_discovery=True):
        return _Resource(recorder, responses)

    class MediaFileUpload:
        def __init__(self, path, chunksize=None, resumable=False):
            self.path = path

    discovery.build = build
    http.MediaFileUpload = MediaFileUpload
    package.discovery = discovery
    package.http = http
    sys.modules["googleapiclient"] = package
    sys.modules["googleapiclient.discovery"] = discovery
    sys.modules["googleapiclient.http"] = http


class YouTubeClientTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: sys.modules.get(k) for k in
                      ("googleapiclient", "googleapiclient.discovery", "googleapiclient.http")}
        self.recorder: list[tuple[str, dict]] = []
        self.responses = {
            "videos.insert": {"id": "abc123"},
            "videos.list": {"items": [{
                "id": "abc123",
                "snippet": {"channelId": "chan", "title": "t", "description": "d",
                            "publishedAt": "2026-01-01", "tags": ["x"], "thumbnails": {}},
                "statistics": {"viewCount": "500", "likeCount": "20", "commentCount": "3"},
                "contentDetails": {"duration": "PT5M"},
            }]},
            "search.list": {"items": [{"id": {"videoId": "abc123"}}, {"id": {"kind": "channel"}}]},
            "reports.query": {"columnHeaders": [{"name": "views"}, {"name": "averageViewPercentage"}],
                              "rows": [[1200, 41.5]]},
        }
        _install_google(self.responses, self.recorder)
        self.store = Store()
        self.ledger = QuotaLedger(self.store)
        from aicut.intelligence.youtube import YouTubeClient

        self.client = YouTubeClient(credentials=object(), ledger=self.ledger)

    def tearDown(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self.store.close()

    def _sent(self, key: str) -> dict:
        return next(kwargs for name, kwargs in self.recorder if name == key)

    def test_upload_trims_the_fields_youtube_rejects_when_too_long(self):
        from aicut.intelligence.youtube import UploadResult

        result = self.client.upload(
            "/video.mp4",
            {"title": "가" * 250, "description": "설명" * 4000, "tags": [f"tag{i}" for i in range(60)]},
        )
        body = self._sent("videos.insert")["body"]

        self.assertIsInstance(result, UploadResult)
        self.assertEqual(len(body["snippet"]["title"]), 100)
        self.assertEqual(len(body["snippet"]["description"]), 5000)
        self.assertEqual(len(body["snippet"]["tags"]), 30)

    def test_upload_defaults_to_private_and_declares_not_made_for_kids(self):
        self.client.upload("/video.mp4", {"title": "t"})
        status = self._sent("videos.insert")["body"]["status"]
        self.assertEqual(status["privacyStatus"], "private")
        self.assertFalse(status["selfDeclaredMadeForKids"])

    def test_a_resumable_upload_runs_until_the_response_arrives(self):
        result = self.client.upload("/video.mp4", {"title": "t"})
        self.assertEqual(result.video_id, "abc123")
        self.assertEqual(result.url, "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(sum(1 for name, _ in self.recorder if name == "videos.insert"), 2)

    def test_an_upload_books_its_quota_only_once(self):
        self.client.upload("/video.mp4", {"title": "t"})
        self.assertEqual(self.ledger.state().used, COST_VIDEO_INSERT)

    def test_an_unaffordable_upload_is_refused_before_any_bytes_move(self):
        self.ledger.spend(COST_VIDEO_INSERT * 6, "earlier uploads")
        with self.assertRaises(QuotaExceeded) as raised:
            self.client.upload("/video.mp4", {"title": "t"})
        self.assertIn("PT midnight", str(raised.exception))
        self.assertEqual(self.recorder, [], "the request must not be issued at all")

    def test_public_metrics_carry_only_public_numbers(self):
        rows = self.client.public_metrics(["abc123"])
        self.assertEqual(rows[0]["public_metrics"], {"views": 500, "likes": 20, "comments": 3})
        self.assertEqual(set(rows[0]) & {"averageViewDuration", "audienceWatchRatio"}, set())

    def test_public_metrics_batches_by_fifty(self):
        self.client.public_metrics([f"v{i}" for i in range(120)])
        calls = [kwargs for name, kwargs in self.recorder if name == "videos.list"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(calls[0]["id"].split(",")), 50)

    def test_search_ignores_results_that_are_not_videos(self):
        self.assertEqual(self.client.search("스트리머 편집"), ["abc123"])

    def test_analytics_asks_only_about_the_owner_channel(self):
        """4.2: this data exists for your own channel and nowhere else."""
        metrics = self.client.analytics("abc123", "2026-01-01", "2026-02-01")
        self.assertEqual(metrics, {"views": 1200, "averageViewPercentage": 41.5})
        sent = self._sent("reports.query")
        self.assertEqual(sent["ids"], "channel==MINE")
        self.assertEqual(sent["filters"], "video==abc123")

    def test_retention_asks_for_the_elapsed_ratio_dimension(self):
        self.responses["reports.query"] = {
            "columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
            "rows": [[0.0, 1.0], [0.5, 0.6]],
        }
        curve = self.client.audience_retention("abc123", "2026-01-01", "2026-02-01")
        self.assertEqual(len(curve), 2)
        self.assertEqual(self._sent("reports.query")["dimensions"], "elapsedVideoTimeRatio")

    def test_making_a_video_public_costs_less_than_uploading_one(self):
        self.client.set_privacy("abc123", "public")
        self.assertLess(self.ledger.state().used, COST_VIDEO_INSERT)
        self.assertEqual(self._sent("videos.update")["body"]["status"]["privacyStatus"], "public")

    def test_setting_a_thumbnail_books_its_own_quota(self):
        self.client.set_thumbnail("abc123", "/thumb.png")
        self.assertEqual(self.ledger.state().used, 50)


if __name__ == "__main__":
    unittest.main()
