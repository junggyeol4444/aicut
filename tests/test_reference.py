"""Loop A and the API boundary it must not cross (4.2, 4.6, 12.3 A)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicut.db.store import Store
from aicut.errors import QuotaExceeded
from aicut.intelligence import reference
from aicut.intelligence.knowledge import ProductionKnowledge
from aicut.intelligence.quota import COST_SEARCH, QuotaLedger
from aicut.llm.mock import MockProducer
from tests.fake_youtube import FakeYouTubeClient


def _catalogue(count: int = 3) -> dict:
    return {
        f"vid{i}": {
            "video_id": f"vid{i}",
            "channel_id": "chan",
            "title": f"게임 스트리머 편집 영상 {i}",
            "description": "highlight edit",
            "tags": ["편집"],
            "duration": "PT10M",
            "published_at": "2026-01-01T00:00:00Z",
            "public_metrics": {"views": 1000 * (i + 1), "likes": 10 * i, "comments": i},
        }
        for i in range(count)
    }


class ReferenceLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        self.ledger = QuotaLedger(self.store)
        self.client = FakeYouTubeClient(self.ledger, catalogue=_catalogue())

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_collection_returns_public_metrics_and_books_quota(self):
        before = self.ledger.state().remaining
        refs = reference.collect_references(self.client, ["게임 스트리머 편집 영상"], per_query=10)

        self.assertEqual(len(refs), 3)
        self.assertIn("views", refs[0]["public_metrics"])
        self.assertNotIn("averageViewPercentage", refs[0]["public_metrics"],
                         "retention is not available for other channels (4.2)")
        self.assertLess(self.ledger.state().remaining, before)

    def test_duplicate_ids_across_queries_are_fetched_once(self):
        refs = reference.collect_references(
            self.client, ["게임 스트리머 편집 영상", "게임 스트리머 편집 영상"], per_query=10
        )
        self.assertEqual(len({r["video_id"] for r in refs}), 3)
        self.assertEqual(len(refs), 3)

    def test_a_failing_query_does_not_abort_the_rest(self):
        class Flaky(FakeYouTubeClient):
            def search(self, query, *, max_results=25, **params):
                if query == "boom":
                    raise RuntimeError("search exploded")
                return super().search(query, max_results=max_results, **params)

        flaky = Flaky(self.ledger, catalogue=_catalogue())
        refs = reference.collect_references(flaky, ["boom", "게임 스트리머 편집 영상"], per_query=10)
        self.assertEqual(len(refs), 3)

    def test_quota_exhaustion_surfaces_rather_than_silently_returning_nothing(self):
        self.ledger.spend(self.ledger.daily_limit - COST_SEARCH + 1, "earlier work")
        with self.assertRaises(QuotaExceeded):
            self.client.search("게임 스트리머 편집 영상")

    def test_analysis_stores_patterns_and_no_media(self):
        """4.6: patterns are kept, reference media is not - and cannot be."""
        refs = reference.collect_references(self.client, ["게임 스트리머 편집 영상"], per_query=10)
        reference.analyze(MockProducer(), self.store, refs)

        rows = self.store.references()
        self.assertEqual(len(rows), 3)
        self.assertIn("production_logic", rows[0]["extracted_patterns"])
        self.assertEqual(rows[0]["public_metrics"]["views"], 1000)
        columns = {row[1] for row in self.store.conn.execute("PRAGMA table_info(tb_yt_reference)")}
        self.assertFalse({"media", "media_path", "video_file", "local_copy"} & columns)

    def test_extra_context_is_passed_to_the_analysis_but_never_stored(self):
        refs = reference.collect_references(self.client, ["게임 스트리머 편집 영상"], per_query=10)
        seen = {}

        class Watching(MockProducer):
            def _task_analyze_reference(self, payload):
                seen.update(payload.get("context", {}))
                return super()._task_analyze_reference(payload)

        reference.analyze(
            Watching(), self.store, refs,
            extra_context={"vid0": {"transcript": "my own notes on this video"}},
        )
        self.assertEqual(seen, {"transcript": "my own notes on this video"})
        for row in self.store.references():
            self.assertNotIn("transcript", str(row["extracted_patterns"]))

    def test_an_analysis_failure_skips_that_reference_only(self):
        refs = reference.collect_references(self.client, ["게임 스트리머 편집 영상"], per_query=10)

        class Grumpy(MockProducer):
            def _task_analyze_reference(self, payload):
                if payload["video"]["title"].endswith("1"):
                    raise RuntimeError("analysis blew up")
                return super()._task_analyze_reference(payload)

        analyses = reference.analyze(Grumpy(), self.store, refs)
        self.assertEqual(len(analyses), 2)
        self.assertEqual(len(self.store.references()), 2)

    def test_knowledge_is_built_from_what_the_references_share(self):
        refs = reference.collect_references(self.client, ["게임 스트리머 편집 영상"], per_query=10)

        class Patterned(MockProducer):
            def _task_analyze_reference(self, payload):
                return {"structure": {"opening": "result first"}, "editing": {"pace": "fast"},
                        "title_pattern": "question", "storytelling": {}, "scene_selection": {},
                        "thumbnail_pattern": "face", "production_logic": "hook then explain"}

        reference.analyze(Patterned(), self.store, refs)
        knowledge = reference.build_knowledge(self.store)

        self.assertEqual(knowledge.sample_size, 3)
        top = knowledge.structure_patterns[0]
        self.assertEqual(top["support"], 3)
        self.assertEqual(top["share"], 1.0)

        summary = knowledge.summary_for_planner()
        self.assertIn("not rules", summary["caveat"])

    def test_knowledge_round_trips_through_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge.json"
            ProductionKnowledge(sample_size=4, title_patterns=["question"]).save(path)
            loaded = ProductionKnowledge.load(path)
            self.assertEqual(loaded.sample_size, 4)
            self.assertEqual(loaded.title_patterns, ["question"])

        self.assertEqual(ProductionKnowledge.load(Path(tmp) / "gone.json").sample_size, 0)


if __name__ == "__main__":
    unittest.main()
