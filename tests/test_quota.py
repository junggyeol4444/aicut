import unittest
from datetime import datetime, timedelta, timezone

from aicut.db.store import Store
from aicut.intelligence.quota import COST_VIDEO_INSERT, QuotaLedger


class QuotaTests(unittest.TestCase):
    """11.4: the corrected quota facts, expressed as behaviour."""

    def setUp(self):
        self.store = Store()
        self.ledger = QuotaLedger(self.store, daily_limit=10000)

    def tearDown(self):
        self.store.close()

    def test_default_quota_allows_about_six_uploads_a_day(self):
        self.assertEqual(self.ledger.uploads_left_today(), 6)

    def test_spending_reduces_the_remaining_allowance(self):
        self.ledger.spend(COST_VIDEO_INSERT, "videos.insert")
        self.assertEqual(self.ledger.uploads_left_today(), 5)
        self.assertEqual(self.ledger.state().used, COST_VIDEO_INSERT)

    def test_the_seventh_upload_of_the_day_is_unaffordable(self):
        for _ in range(6):
            self.ledger.spend(COST_VIDEO_INSERT, "videos.insert")
        self.assertFalse(self.ledger.can_afford(COST_VIDEO_INSERT))
        self.assertEqual(self.ledger.uploads_left_today(), 0)

    def test_retry_targets_pt_midnight_not_24_hours_later(self):
        late = datetime(2026, 3, 10, 23, 30, tzinfo=timezone.utc)     # 16:30 PT
        reset = self.ledger.next_reset(after=late)
        self.assertEqual((reset.hour, reset.minute), (0, 0))
        self.assertLess(reset - late, timedelta(hours=24))
        self.assertGreater(reset, late)

    def test_usage_is_booked_against_the_pacific_day(self):
        date = self.ledger.pt_date()
        self.ledger.spend(500, "search.list")
        self.assertEqual(self.store.quota_used(date), 500)
        self.assertEqual(self.store.quota_used("1999-01-01"), 0)


class UploadQueueTests(unittest.TestCase):
    """16장: a quota failure keeps the video locally and schedules a retry."""

    def test_queue_round_trip(self):
        from aicut.models import Episode, Project

        store = Store()
        project = store.create_project(Project(file_path="/f.mkv"))
        episode = store.save_episode(Episode(project_id=project.project_id))
        reset = QuotaLedger(store).next_reset().isoformat()
        store.enqueue_upload(episode.episode_id, reset, "quota exhausted")

        queued = store.upload_queue()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["retry_after"], reset)

        store.set_queue_state(queued[0]["queue_id"], "uploaded")
        self.assertEqual(store.upload_queue(), [])
        store.close()


if __name__ == "__main__":
    unittest.main()
