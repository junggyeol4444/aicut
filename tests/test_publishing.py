"""Upload, the human gate and the quota retry queue (11.3, 11.4, 16장).

Run against a fake YouTube client that books quota through the real ledger, so
exhaustion, queueing and the Pacific reset behave as they would in production.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import QuotaExceeded
from aicut.intelligence.quota import COST_VIDEO_INSERT, QuotaLedger
from aicut.llm import get_producer
from aicut.models import Episode, Project
from aicut.pipeline import performance, publishing, review
from aicut.pipeline.context import RunContext
from tests.fake_youtube import FakeYouTubeClient


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = Store(self.dir / "db.sqlite")
        self.profile = CalibrationProfile.load()
        self.ledger = QuotaLedger(self.store)
        self.client = FakeYouTubeClient(self.ledger)
        self.project = self.store.create_project(Project(file_path="/f.mkv", duration_sec=100))
        self.ctx = RunContext(
            project=self.project, store=self.store, profile=self.profile,
            producer=get_producer("mock"), workspace=self.dir,
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _episode(self, *, rendered: bool = True, thumb: bool = True) -> Episode:
        video = self.dir / "out.mp4"
        video.write_bytes(b"video")
        thumbnail = self.dir / "thumb.png"
        thumbnail.write_bytes(b"png")
        episode = Episode(
            project_id=self.project.project_id,
            title_candidates=["a real title", "alt", "alt2"],
            metadata={"description": "what happened", "tags": ["tournament"]},
            output_mp4_path=str(video) if rendered else None,
            thumbnail_candidates=[str(thumbnail)] if thumb else [],
        )
        return self.store.save_episode(episode)

    # -- 11.3 -------------------------------------------------------------
    def test_upload_lands_private_and_waits_for_a_person(self):
        episode = self._episode()
        result = publishing.upload_episode(self.ctx, episode, self.client)

        self.assertEqual(result["privacy_status"], "private")
        self.assertEqual(self.client.uploaded[0]["metadata"]["title"], "a real title")
        stored = self.store.get_episode(episode.episode_id)
        self.assertEqual(stored.review_status, "pending")
        self.assertEqual(self.client.privacy[result["video_id"]], "private")

    def test_the_first_thumbnail_candidate_is_attached(self):
        episode = self._episode()
        result = publishing.upload_episode(self.ctx, episode, self.client)
        self.assertIn(result["video_id"], self.client.thumbnails)

    def test_an_unrendered_episode_cannot_be_uploaded(self):
        with self.assertRaises(ValueError):
            publishing.upload_episode(self.ctx, self._episode(rendered=False), self.client)

    def test_publishing_requires_the_gate_then_goes_public(self):
        episode = self._episode()
        publishing.upload_episode(self.ctx, episode, self.client)
        episode = self.store.get_episode(episode.episode_id)

        with self.assertRaises(PermissionError):
            publishing.publish_approved(self.ctx, episode, self.client)

        review.approve(self.ctx, episode.episode_id, reviewer="junggyeol", note="ok")
        approved = self.store.get_episode(episode.episode_id)
        published = publishing.publish_approved(self.ctx, approved, self.client)

        self.assertEqual(published.review_status, "published")
        self.assertEqual(self.client.privacy[published.metadata["youtube"]["video_id"]], "public")

    def test_a_rejected_episode_stays_unpublishable(self):
        episode = self._episode()
        publishing.upload_episode(self.ctx, episode, self.client)
        review.reject(self.ctx, episode.episode_id, reviewer="junggyeol", reason="the joke does not land")
        with self.assertRaises(PermissionError):
            publishing.publish_approved(self.ctx, self.store.get_episode(episode.episode_id), self.client)

    def test_publishing_before_uploading_is_refused(self):
        episode = self._episode()
        review.approve(self.ctx, episode.episode_id, reviewer="junggyeol")
        with self.assertRaises(ValueError):
            publishing.publish_approved(self.ctx, self.store.get_episode(episode.episode_id), self.client)

    # -- 11.4 / 16장 ------------------------------------------------------
    def test_quota_exhaustion_queues_the_episode_against_the_pacific_reset(self):
        self.ledger.spend(COST_VIDEO_INSERT * 6, "earlier uploads")
        episode = self._episode()

        with self.assertRaises(QuotaExceeded) as raised:
            publishing.upload_episode(self.ctx, episode, self.client)

        self.assertIn("PT midnight", str(raised.exception))
        queued = self.store.upload_queue()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["episode_id"], episode.episode_id)
        self.assertEqual(queued[0]["retry_after"], self.ledger.next_reset().isoformat())
        self.assertEqual(self.store.get_episode(episode.episode_id).review_status, "upload_queued")
        self.assertEqual(self.client.uploaded, [], "the video must stay local, not half-uploaded")

    def test_the_retry_queue_waits_until_the_reset_then_uploads(self):
        self.ledger.spend(COST_VIDEO_INSERT * 6, "earlier uploads")
        episode = self._episode()
        with self.assertRaises(QuotaExceeded):
            publishing.upload_episode(self.ctx, episode, self.client)

        # Still the same Pacific day: nothing may go out yet.
        self.assertEqual(publishing.process_retry_queue(self.ctx, self.client, self.ledger), [])
        self.assertEqual(self.client.uploaded, [])

        # Tomorrow: the ledger is empty again and the queue drains.
        tomorrow = QuotaLedger(self.store)
        tomorrow.pt_date = lambda: (self.ledger.pt_now() + timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow.pt_now = lambda: self.ledger.pt_now() + timedelta(days=1)
        self.client.ledger = tomorrow

        done = publishing.process_retry_queue(self.ctx, self.client, tomorrow)
        self.assertEqual(done, [episode.episode_id])
        self.assertEqual(len(self.client.uploaded), 1)
        self.assertEqual(self.store.upload_queue(), [])

    def test_deleting_an_episode_takes_its_queue_entry_with_it(self):
        episode = self._episode()
        self.store.enqueue_upload(episode.episode_id, None, "quota")
        self.store.conn.execute("DELETE FROM tb_episode WHERE episode_id=?", (episode.episode_id,))
        self.store.conn.commit()

        self.assertEqual(self.store.upload_queue(), [], "the cascade should have removed the orphan")
        self.assertEqual(publishing.process_retry_queue(self.ctx, self.client, self.ledger), [])

    def test_a_retry_that_hits_the_quota_again_updates_its_row_rather_than_stacking(self):
        episode = self._episode()
        self.store.enqueue_upload(episode.episode_id, None, "quota")
        self.ledger.spend(COST_VIDEO_INSERT * 6, "earlier uploads")

        for _ in range(3):
            publishing.process_retry_queue(self.ctx, self.client, self.ledger)

        rows = self.store.upload_queue()
        self.assertEqual(len(rows), 1, "each drain attempt queued the same episode again")
        self.assertIn("PT midnight", rows[0]["last_error"])

    def test_the_queue_stops_as_soon_as_the_day_runs_out_again(self):
        episodes = [self._episode() for _ in range(3)]
        for episode in episodes:
            self.store.enqueue_upload(episode.episode_id, None, "quota")
        self.ledger.spend(COST_VIDEO_INSERT * 5, "earlier uploads")   # room for exactly one

        done = publishing.process_retry_queue(self.ctx, self.client, self.ledger)
        self.assertEqual(len(done), 1)
        self.assertEqual(len(self.store.upload_queue()), 2, "the rest must stay queued, not be dropped")


class PerformanceLoopTests(unittest.TestCase):
    """12.3 C, and the 4.2 rule that retention exists only for your own videos."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = Store(self.dir / "db.sqlite")
        self.ledger = QuotaLedger(self.store)
        self.client = FakeYouTubeClient(self.ledger)
        project = self.store.create_project(Project(file_path="/f.mkv"))
        self.ctx = RunContext(
            project=project, store=self.store, profile=CalibrationProfile.load(),
            producer=get_producer("mock"), workspace=self.dir,
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _published_episode(self) -> Episode:
        video = self.dir / "out.mp4"
        video.write_bytes(b"video")
        episode = self.store.save_episode(Episode(
            project_id=self.ctx.project.project_id, output_mp4_path=str(video),
            title_candidates=["t"], planned_structure={"structure_name": "result_first"},
            target_type="long",
        ))
        publishing.upload_episode(self.ctx, episode, self.client)
        episode = self.store.get_episode(episode.episode_id)
        review.approve(self.ctx, episode.episode_id, reviewer="junggyeol")
        return publishing.publish_approved(self.ctx, self.store.get_episode(episode.episode_id), self.client)

    def test_metrics_are_collected_only_for_published_episodes(self):
        published = self._published_episode()
        self.store.save_episode(Episode(project_id=self.ctx.project.project_id))   # never published

        collected = performance.collect(self.ctx, self.client)
        self.assertEqual([row["episode_id"] for row in collected], [published.episode_id])
        self.assertIn("retention_curve", collected[0]["metrics"])
        self.assertEqual(len(self.store.performance(published.episode_id)), 1)

    def test_retention_is_refused_for_a_video_that_is_not_ours(self):
        with self.assertRaises(PermissionError):
            self.client.audience_retention("someone_elses_video", "2026-01-01", "2026-02-01")

    def test_learning_folds_the_metrics_into_the_knowledge_file(self):
        self._published_episode()
        performance.collect(self.ctx, self.client)
        knowledge_path = self.dir / "knowledge.json"

        result = performance.learn(self.ctx, knowledge_path)
        self.assertIn("observations", result)
        self.assertTrue(knowledge_path.exists())

    def test_learning_with_no_data_returns_empty_rather_than_inventing(self):
        self.assertEqual(performance.learn(self.ctx), {"observations": [], "strategy_updates": []})


if __name__ == "__main__":
    unittest.main()
